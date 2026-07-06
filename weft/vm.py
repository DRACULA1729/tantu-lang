"""The stack VM + cooperative fiber scheduler (DESIGN.md §4, §5.4, §5.5).

One OS thread. `execute(fiber)` runs a fiber for up to `budget` instructions, returning a
signal (DONE / PARKED / BUDGET_EXHAUSTED); `run()` is the scheduler loop over a FIFO ready
deque plus a timer heap. Concurrency is cooperative and fully deterministic given the injected
clock, so concurrent programs are golden-testable (Fable M7 rulings). Blocking exists only at
CHAN_SEND / CHAN_RECV (channel wait deques) and SLEEP (timer heap); `ip` is advanced before an
op body runs and never rewound — a parking op leaves the fiber ready to resume at the next
instruction with its result already on (or deposited onto) the stack.
"""

from __future__ import annotations

import enum
import sys
import time
from collections import deque
from heapq import heappush, heappop

from weft.bytecode import Op, Code
from weft.errors import (
    Span, WeftPanic, render_panic, render_span,
    R_DIV_BY_ZERO, R_UNINIT, R_MATCH_FAIL, R_INDEX_OUT_OF_RANGE,
    R_SEND_CLOSED, R_CLOSE_CLOSED, R_DEADLOCK, R_STEP_LIMIT, R_BUILTIN,
    R_STACK_OVERFLOW, R_BAD_CAP,
)
from weft.values import render, Closure, Builtin, Variant, Record, Channel
from weft.prelude import builtin_impls

_UNINIT = object()   # sentinel for an unread global/local slot (checker guarantees def-before-use)

_TRACE_FRAMES = 20   # panic traces render at most this many innermost frames (then a count)


class _Sig(enum.Enum):
    DONE = 1            # fiber finished (HALT for main; last RETURN for a spawned fiber)
    PARKED = 2          # a channel/timer wait structure now holds the fiber
    BUDGET = 3          # preemption budget exhausted; requeue


class State(enum.Enum):
    READY = 1
    RUNNING = 2
    PARKED = 3
    DONE = 4


def _some(v) -> Variant:
    return Variant(0, "Option", "Some", (v,))


_NONE = Variant(1, "Option", "None", ())


class _SelectGroup:
    """Shared state for one `select` that parked: `won` flips true when any arm fires. `entries`
    records `(wait_deque, waiter)` per registered arm so a winning arm can eagerly dequeue its
    siblings from the other channels (`_cancel_select`); the lazy tombstone skim in the pop-live
    paths remains as a second line of defense (M9a)."""

    __slots__ = ("won", "entries")

    def __init__(self) -> None:
        self.won = False
        self.entries: list = []      # list[tuple[deque, _SelectWaiter]]


class _SelectWaiter:
    """A select's registration on one channel's wait deque. Sits alongside bare fibers (recv) or
    (fiber, value) tuples (send); a counterparty completes it via `_resolve_recv`/`_resolve_send`,
    depositing `(case_index, payload)` so the woken fiber's SELECT_DISPATCH jumps to the arm."""

    __slots__ = ("fiber", "case_index", "value", "group")

    def __init__(self, fiber: "Fiber", case_index: int, value, group: _SelectGroup) -> None:
        self.fiber = fiber
        self.case_index = case_index
        self.value = value          # the send payload for a send arm; None for a recv arm
        self.group = group


class VMError(RuntimeError):
    """Internal error: an opcode reached the VM with no handler. Signals a compiler bug."""


class Frame:
    __slots__ = ("code", "ip", "locals", "closure", "stack_base")

    def __init__(self, code: Code, closure: object = None, stack_base: int = 0) -> None:
        self.code = code
        self.ip = 0
        self.locals: list = [_UNINIT] * code.n_locals
        self.closure = closure
        self.stack_base = stack_base


class Fiber:
    __slots__ = ("fid", "frames", "stack", "state", "pending_panic")

    def __init__(self, fid: int, frame: Frame) -> None:
        self.fid = fid
        self.frames: list[Frame] = [frame]
        self.stack: list = []
        self.state = State.READY
        self.pending_panic: WeftPanic | None = None   # e.g. a parked sender woken by close


class VM:
    def __init__(self, budget: int = 1000, stdout=None,
                 clock=time.monotonic, sleep_fn=time.sleep,
                 max_frames: int = 100000) -> None:
        self.budget = budget
        self.max_frames = max_frames            # Weft call-stack depth cap (R010 on overflow)
        self.stdout = stdout if stdout is not None else sys.stdout
        self.clock = clock                       # injected; tests use a virtual clock
        self.sleep_fn = sleep_fn                 # injected idle sleep
        self.builtins = builtin_impls(render)    # name -> Python impl (dispatched by CALL)
        self.step_limit: int | None = None       # set per-run; None = unlimited (see run())
        self._steps = 0

    # -- scheduler -----------------------------------------------------------

    def run(self, module_code: Code, src: str = "", file: str = "<unknown>",
            *, globals: list | None = None, srcs: dict | None = None,
            step_limit: int | None = None) -> int:
        """Run a program to completion. 0 = success, 1 = panic/deadlock (DESIGN.md §6.2).

        `srcs` maps file→source for multi-module panic rendering; a single-module run passes
        `src`/`file` and gets `{file: src}`.

        `step_limit` optionally caps the TOTAL instructions executed across all fibers; on
        overflow the VM raises a graceful panic (R008) instead of looping forever. Defaults to
        None (unlimited) — behavior is unchanged for every existing caller.
        """
        self.step_limit = step_limit
        self._steps = 0
        if globals is None:
            self.globals = [_UNINIT] * module_code.n_globals
        else:
            while len(globals) < module_code.n_globals:
                globals.append(_UNINIT)
            self.globals = globals
        self.srcs = srcs if srcs is not None else {file: src}
        self.ready: deque[Fiber] = deque()
        self.timers: list = []          # heap of (wake_at, seq, fiber)
        self.seq = 1                    # monotonic: spawned fids AND timer tie-breaks (main = 0)
        self.registry: dict[int, Fiber] = {}    # fid -> live fiber; DONE fibers are pruned (O(1))
        self.current: Fiber | None = None

        main = Fiber(0, Frame(module_code))
        self.registry[main.fid] = main
        self.ready.append(main)
        try:
            while True:
                now = self.clock()
                while self.timers and self.timers[0][0] <= now:      # wake due timers → back of ready
                    _, _, f = heappop(self.timers)
                    f.state = State.READY
                    self.ready.append(f)
                if not self.ready:
                    if self.timers:
                        self.sleep_fn(max(0.0, self.timers[0][0] - self.clock()))
                        continue
                    parked = [f for f in self.registry.values() if f.state is State.PARKED]
                    if parked:
                        self._render_deadlock(parked)
                        return 1
                    return 0
                fib = self.ready.popleft()
                if fib.state is State.DONE:
                    continue
                fib.state = State.RUNNING
                self.current = fib
                sig = self.execute(fib)
                if sig is _Sig.BUDGET:
                    fib.state = State.READY
                    self.ready.append(fib)
                elif sig is _Sig.PARKED:
                    fib.state = State.PARKED
                else:  # DONE
                    fib.state = State.DONE
                    self.registry.pop(fib.fid, None)     # prune: registry holds live fibers only
                    if fib.fid == 0:
                        return 0             # main done → program exits, other fibers dropped
        except WeftPanic as p:
            trace = self._trace(self.current, p)
            text = render_panic(self.srcs, trace[:_TRACE_FRAMES], p)
            if len(trace) > _TRACE_FRAMES:   # runaway stacks: innermost frames + a count
                text += f"\n  … {len(trace) - _TRACE_FRAMES} more frames"
            self.stdout.write(text + "\n")
            return 1

    def _seq(self) -> int:
        s = self.seq
        self.seq += 1
        return s

    def _wake(self, f: Fiber, value) -> None:
        f.stack.append(value)                    # deposit the awaited result
        f.state = State.READY
        self.ready.append(f)

    # -- channel wait deques: skim tombstoned select waiters, complete the live one (M9a) --

    def _pop_live_receiver(self, c: Channel):
        """Pop the next live receiver (a bare Fiber or a _SelectWaiter), discarding tombstones."""
        while c.recv_wait:
            r = c.recv_wait.popleft()
            if isinstance(r, Fiber) or not r.group.won:
                return r
        return None

    def _pop_live_sender(self, c: Channel):
        """Pop the next live sender ((Fiber, value) tuple or _SelectWaiter), discarding tombstones."""
        while c.send_wait:
            s = c.send_wait.popleft()
            if isinstance(s, tuple) or not s.group.won:
                return s
        return None

    def _cancel_select(self, group: _SelectGroup) -> None:
        """A select arm fired: mark the group won and eagerly dequeue its sibling waiters from
        every channel they were registered on, so non-firing channels don't accumulate
        tombstones (the lazy skim in the pop-live paths stays as a backstop)."""
        group.won = True
        for dq, w in group.entries:
            try:
                dq.remove(w)
            except ValueError:
                pass                             # already popped (e.g. the winning arm itself)
        group.entries.clear()

    def _resolve_recv(self, r, value) -> None:
        """Deliver an Option `value` to a receiver record — plain fiber or select waiter."""
        if isinstance(r, Fiber):
            self._wake(r, value)                                 # plain recv: the Option itself
        else:
            self._cancel_select(r.group)
            self._wake(r.fiber, (r.case_index, value))           # select: the dispatch pair

    def _resolve_send(self, s):
        """Complete a sender record and return the value it sent (for buffering/handoff)."""
        if isinstance(s, tuple):
            f, v = s
            self._wake(f, None)                                  # plain send: Unit
            return v
        self._cancel_select(s.group)
        self._wake(s.fiber, (s.case_index, None))                # select send arm: Unit payload
        return s.value

    def _trace(self, fiber: Fiber, panic: WeftPanic) -> list[tuple[str, Span | None]]:
        trace: list[tuple[str, Span | None]] = []
        for frame in reversed(fiber.frames):
            idx = frame.ip - 1
            span = frame.code.spans[idx] if 0 <= idx < len(frame.code.spans) else None
            if not trace and panic.span is not None:
                span = panic.span
            trace.append((frame.code.name, span))
        return trace

    def _render_deadlock(self, parked: list[Fiber]) -> None:
        lines = [f"panic [{R_DEADLOCK}]: deadlock — every fiber is blocked"]
        for f in parked:
            frame = f.frames[-1]
            span = frame.code.spans[frame.ip - 1] if frame.code.spans else None
            if span is not None:
                lines.append(f"  fiber {f.fid} blocked in {frame.code.name} "
                             f"at {span.file}:{span.line}:{span.col + 1}")
                src = self.srcs.get(span.file, "")
                if src:
                    lines.extend(render_span(src, span))
        self.stdout.write("\n".join(lines) + "\n")

    # -- the instruction loop ------------------------------------------------

    def execute(self, fiber: Fiber) -> _Sig:
        if fiber.pending_panic is not None:      # e.g. a parked sender woken by close(chan)
            p, fiber.pending_panic = fiber.pending_panic, None
            raise p
        stack = fiber.stack
        for _ in range(self.budget):
            if self.step_limit is not None:
                self._steps += 1
                if self._steps > self.step_limit:
                    raise WeftPanic(R_STEP_LIMIT,
                                    f"execution step limit reached ({self.step_limit} instructions)")
            frame = fiber.frames[-1]             # re-fetch: CALL/RETURN/SPAWN swap frames
            instr = frame.code.instrs[frame.ip]
            frame.ip += 1
            op = instr.op

            if op is Op.LOAD_CONST:
                stack.append(frame.code.consts[instr.arg])
            elif op is Op.POP:
                stack.pop()
            elif op is Op.ADD:
                b = stack.pop(); a = stack.pop(); stack.append(a + b)
            elif op is Op.SUB:
                b = stack.pop(); a = stack.pop(); stack.append(a - b)
            elif op is Op.MUL:
                b = stack.pop(); a = stack.pop(); stack.append(a * b)
            elif op is Op.DIV:
                b = stack.pop(); a = stack.pop()
                if b == 0:
                    raise WeftPanic(R_DIV_BY_ZERO, "division or modulo by zero",
                                    frame.code.spans[frame.ip - 1])
                stack.append(a // b if (isinstance(a, int) and isinstance(b, int)) else a / b)
            elif op is Op.MOD:
                b = stack.pop(); a = stack.pop()
                if b == 0:
                    raise WeftPanic(R_DIV_BY_ZERO, "division or modulo by zero",
                                    frame.code.spans[frame.ip - 1])
                stack.append(a % b)
            elif op is Op.NEG:
                stack.append(-stack.pop())
            elif op is Op.LOAD_GLOBAL:
                v = self.globals[instr.arg]
                if v is _UNINIT:
                    raise WeftPanic(R_UNINIT, "value used before initialization",
                                    frame.code.spans[frame.ip - 1])
                stack.append(v)
            elif op is Op.STORE_GLOBAL:
                self.globals[instr.arg] = stack.pop()
            elif op is Op.LOAD_LOCAL:
                v = frame.locals[instr.arg]
                if v is _UNINIT:
                    raise WeftPanic(R_UNINIT, "value used before initialization",
                                    frame.code.spans[frame.ip - 1])
                stack.append(v)
            elif op is Op.STORE_LOCAL:
                frame.locals[instr.arg] = stack.pop()
            elif op is Op.LOAD_CAPTURE:
                stack.append(frame.closure.captures[instr.arg])
            elif op is Op.MAKE_CLOSURE:
                code_idx, ncap = instr.arg
                caps = tuple(stack[-ncap:]) if ncap else ()
                if ncap:
                    del stack[-ncap:]
                stack.append(Closure(frame.code.consts[code_idx], caps))
            elif op is Op.CALL:
                argc = instr.arg
                base = len(stack) - argc - 1
                callee = stack[base]
                if isinstance(callee, Closure):
                    if len(fiber.frames) >= self.max_frames:
                        raise WeftPanic(R_STACK_OVERFLOW,
                                        f"call stack overflow ({self.max_frames} frames)",
                                        frame.code.spans[frame.ip - 1])
                    code = callee.code
                    args = stack[base + 1:]
                    del stack[base:]
                    nf = Frame(code, closure=callee, stack_base=len(stack))
                    nf.locals = args + [_UNINIT] * (code.n_locals - argc)
                    fiber.frames.append(nf)
                elif isinstance(callee, Builtin):
                    args = stack[base + 1:]
                    del stack[base:]
                    # Panic boundary: a host exception inside a builtin becomes a clean
                    # R009 panic at the call site instead of a raw Python traceback.
                    try:
                        result = self.builtins[callee.name](*args)
                    except WeftPanic:
                        raise
                    except Exception as ex:
                        raise WeftPanic(R_BUILTIN, f"{callee.name}: {ex}",
                                        frame.code.spans[frame.ip - 1])
                    stack.append(result)
                else:
                    raise VMError(f"cannot call {type(callee).__name__}")
            elif op is Op.RETURN:
                retval = stack.pop()
                del stack[frame.stack_base:]
                fiber.frames.pop()
                if not fiber.frames:
                    return _Sig.DONE          # a spawned fiber's function returned
                stack.append(retval)
            elif op is Op.MAKE_VARIANT:
                enum_name, variant_name, tag, arity = frame.code.consts[instr.arg]
                fields = tuple(stack[-arity:]) if arity else ()
                if arity:
                    del stack[-arity:]
                stack.append(Variant(tag, enum_name, variant_name, fields))
            elif op is Op.TEST_VARIANT:
                stack.append(stack.pop().tag == instr.arg)
            elif op is Op.UNPACK_VARIANT:
                stack.extend(stack.pop().fields)
            elif op is Op.MATCH_FAIL:
                raise WeftPanic(R_MATCH_FAIL, "non-exhaustive match",
                                frame.code.spans[frame.ip - 1])
            elif op is Op.BUILD_LIST:
                n = instr.arg
                items = list(stack[-n:]) if n else []
                if n:
                    del stack[-n:]
                stack.append(items)
            elif op is Op.BUILD_TUPLE:
                n = instr.arg
                items = tuple(stack[-n:]) if n else ()
                if n:
                    del stack[-n:]
                stack.append(items)
            elif op is Op.INDEX:
                i = stack.pop(); xs = stack.pop()
                if i < 0 or i >= len(xs):
                    raise WeftPanic(R_INDEX_OUT_OF_RANGE, f"list index out of range: {i}",
                                    frame.code.spans[frame.ip - 1])
                stack.append(xs[i])
            elif op is Op.GET_FIELD:
                obj = stack.pop()
                stack.append(obj.fields[instr.arg] if isinstance(obj, Record) else obj[instr.arg])
            elif op is Op.MAKE_RECORD:
                struct_name, arity = frame.code.consts[instr.arg]
                fields = tuple(stack[-arity:]) if arity else ()
                if arity:
                    del stack[-arity:]
                stack.append(Record(struct_name, fields))
            elif op is Op.DUP:
                stack.append(stack[-1])
            elif op is Op.NOT:
                stack.append(not stack.pop())
            elif op is Op.EQ:
                b = stack.pop(); a = stack.pop(); stack.append(a == b)
            elif op is Op.NE:
                b = stack.pop(); a = stack.pop(); stack.append(a != b)
            elif op is Op.LT:
                b = stack.pop(); a = stack.pop(); stack.append(a < b)
            elif op is Op.LE:
                b = stack.pop(); a = stack.pop(); stack.append(a <= b)
            elif op is Op.GT:
                b = stack.pop(); a = stack.pop(); stack.append(a > b)
            elif op is Op.GE:
                b = stack.pop(); a = stack.pop(); stack.append(a >= b)
            elif op is Op.JUMP:
                frame.ip = instr.arg
            elif op is Op.JUMP_IF_FALSE:
                if not stack.pop():
                    frame.ip = instr.arg
            elif op is Op.JUMP_IF_TRUE:
                if stack.pop():
                    frame.ip = instr.arg
            elif op is Op.PRINT:
                self.stdout.write(render(stack.pop()) + "\n")
                stack.append(None)
            # -- concurrency (M7) --------------------------------------------
            elif op is Op.SPAWN:
                argc = instr.arg
                base = len(stack) - argc - 1
                clo = stack[base]
                args = stack[base + 1:]
                del stack[base:]
                code = clo.code
                nf = Fiber(self._seq(), Frame(code, closure=clo, stack_base=0))
                nf.frames[-1].locals = args + [_UNINIT] * (code.n_locals - argc)
                self.registry[nf.fid] = nf
                self.ready.append(nf)          # queued; the acting fiber keeps running
            elif op is Op.CHAN_NEW:
                cap = stack.pop()
                if cap < 0:
                    raise WeftPanic(R_BAD_CAP,
                                    f"channel capacity cannot be negative ({cap})",
                                    frame.code.spans[frame.ip - 1])
                stack.append(Channel(cap))
            elif op is Op.CHAN_SEND:
                v = stack.pop(); c = stack.pop()
                if c.closed:
                    raise WeftPanic(R_SEND_CLOSED, "send on closed channel",
                                    frame.code.spans[frame.ip - 1])
                r = self._pop_live_receiver(c)
                if r is not None:
                    self._resolve_recv(r, _some(v))
                    stack.append(None)
                elif len(c.buf) < c.cap:
                    c.buf.append(v)
                    stack.append(None)
                else:
                    c.send_wait.append((fiber, v))
                    return _Sig.PARKED
            elif op is Op.CHAN_RECV:
                c = stack.pop()
                if c.buf:
                    v = c.buf.popleft()
                    s = self._pop_live_sender(c)          # admit a waiting sender into the buffer
                    if s is not None:
                        c.buf.append(self._resolve_send(s))
                    stack.append(_some(v))
                else:
                    s = self._pop_live_sender(c)
                    if s is not None:                     # unbuffered rendezvous
                        stack.append(_some(self._resolve_send(s)))
                    elif c.closed:
                        stack.append(_NONE)
                    else:
                        c.recv_wait.append(fiber)
                        return _Sig.PARKED
            elif op is Op.CHAN_CLOSE:
                c = stack.pop()
                if c.closed:
                    raise WeftPanic(R_CLOSE_CLOSED, "close of closed channel",
                                    frame.code.spans[frame.ip - 1])
                c.closed = True
                while c.recv_wait:                        # wake receivers with None (drop tombstones)
                    r = c.recv_wait.popleft()
                    if isinstance(r, Fiber):
                        self._wake(r, _NONE)
                    elif not r.group.won:
                        self._resolve_recv(r, _NONE)
                while c.send_wait:                        # parked senders panic on resume
                    s = c.send_wait.popleft()
                    if isinstance(s, tuple):
                        sf = s[0]
                    elif not s.group.won:
                        self._cancel_select(s.group)
                        sf = s.fiber
                    else:
                        continue                          # tombstone: another arm already fired
                    sframe = sf.frames[-1]
                    sf.pending_panic = WeftPanic(R_SEND_CLOSED, "send on closed channel",
                                                 sframe.code.spans[sframe.ip - 1])
                    sf.state = State.READY
                    self.ready.append(sf)
                stack.append(None)
            elif op is Op.SLEEP:
                ms = stack.pop()
                stack.append(None)                        # pre-push the Unit result
                if ms <= 0:
                    return _Sig.BUDGET                    # yield: back of ready, clock untouched
                heappush(self.timers, (self.clock() + ms / 1000.0, self._seq(), fiber))
                return _Sig.PARKED
            elif op is Op.SELECT:
                has_else, *kinds = instr.arg              # kind: 0 = recv, 1 = send
                n = len(kinds)                            # channel-arm count (else index = n)
                widths = [1 if k == 0 else 2 for k in kinds]
                total = sum(widths)
                ops = stack[-total:] if total else []     # pop all operands up front
                if total:
                    del stack[-total:]
                arms = []                                 # (kind, chan, send_value|None)
                pos = 0
                for k, w in zip(kinds, widths):
                    arms.append((k, ops[pos], ops[pos + 1] if w == 2 else None))
                    pos += w
                chosen = None                             # phase 1: first-ready poll, source order
                for i, (k, c, v) in enumerate(arms):
                    if k == 0:                            # recv arm — mirror CHAN_RECV ready paths
                        if c.buf:
                            val = c.buf.popleft()
                            s = self._pop_live_sender(c)
                            if s is not None:
                                c.buf.append(self._resolve_send(s))
                            chosen = (i, _some(val)); break
                        s = self._pop_live_sender(c)
                        if s is not None:                 # unbuffered rendezvous
                            chosen = (i, _some(self._resolve_send(s))); break
                        if c.closed:
                            chosen = (i, _NONE); break     # closed channel → arm fires with None
                    else:                                 # send arm — mirror CHAN_SEND ready paths
                        if c.closed:
                            raise WeftPanic(R_SEND_CLOSED, "send on closed channel",
                                            frame.code.spans[frame.ip - 1])
                        r = self._pop_live_receiver(c)
                        if r is not None:
                            self._resolve_recv(r, _some(v)); chosen = (i, None); break
                        if len(c.buf) < c.cap:
                            c.buf.append(v); chosen = (i, None); break
                if chosen is not None:
                    stack.append(chosen)                  # fall through to SELECT_DISPATCH
                elif has_else:
                    stack.append((n, None))               # else fires; never parks
                else:                                     # phase 2: register on every channel arm
                    group = _SelectGroup()
                    for i, (k, c, v) in enumerate(arms):
                        waiter = _SelectWaiter(fiber, i, v, group)
                        dq = c.recv_wait if k == 0 else c.send_wait
                        dq.append(waiter)
                        group.entries.append((dq, waiter))
                    return _Sig.PARKED
            elif op is Op.SELECT_DISPATCH:
                idx, payload = stack.pop()
                stack.append(payload)
                frame.ip = instr.arg[idx]
            elif op is Op.HALT:
                return _Sig.DONE
            else:
                raise VMError(f"opcode {op.name} has no handler")
        return _Sig.BUDGET
