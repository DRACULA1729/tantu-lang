"""The bytecode contract between compiler and VM (DESIGN.md §5.2, §5.3).

The COMPLETE v1 instruction set is enumerated now with explicit, append-only integer values
so later milestones only wire handlers — never renumber or serialize-shift. M1 emits just the
arithmetic/const/PRINT/POP/HALT subset; the VM raises for any op it has not wired yet.

Instr correction (DECISIONS.md, 2026-07-06): a single `arg` field holds `None`, an `int`, or
a small `tuple[int, ...]` for two-operand ops (MAKE_CLOSURE). This replaces the doc's original
`arg`/`arg2` + `k*256+n` packing, which broke once a const index exceeded 255.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from weft.errors import Span


class Op(enum.IntEnum):
    # loads / stores
    LOAD_CONST = 1
    LOAD_LOCAL = 2
    STORE_LOCAL = 3
    LOAD_GLOBAL = 4
    STORE_GLOBAL = 5
    LOAD_CAPTURE = 6
    # stack
    POP = 7
    DUP = 8
    # arithmetic
    ADD = 9
    SUB = 10
    MUL = 11
    DIV = 12
    MOD = 13
    NEG = 14
    NOT = 15
    # comparison
    EQ = 16
    NE = 17
    LT = 18
    LE = 19
    GT = 20
    GE = 21
    # control flow
    JUMP = 22
    JUMP_IF_FALSE = 23
    JUMP_IF_TRUE = 24
    # functions
    MAKE_CLOSURE = 25
    CALL = 26
    RETURN = 27
    # aggregates
    BUILD_LIST = 28
    BUILD_TUPLE = 29
    INDEX = 30
    GET_FIELD = 31
    MAKE_VARIANT = 32
    MAKE_RECORD = 33
    TEST_VARIANT = 34
    UNPACK_VARIANT = 35
    MATCH_FAIL = 36
    # concurrency
    SPAWN = 37
    CHAN_NEW = 38
    CHAN_SEND = 39
    CHAN_RECV = 40
    CHAN_CLOSE = 41
    SLEEP = 42
    # io / halt
    PRINT = 43
    HALT = 44
    # select (M9a)
    SELECT = 45           # arg = (has_else, kind0, ...); kind: 0 = recv, 1 = send
    SELECT_DISPATCH = 46  # arg = (target0, ..., [target_else]); absolute instr indices


@dataclass(frozen=True, slots=True)
class Instr:
    op: Op
    arg: int | tuple[int, ...] | None = None


@dataclass
class Code:
    """A compiled function or module body — the unit the VM executes (DESIGN.md §5.2)."""

    name: str
    arity: int
    n_locals: int
    n_globals: int
    instrs: list[Instr]
    consts: list[object]
    spans: list[Span]  # parallel to instrs; powers stack traces

    def dis(self) -> str:
        """Human-readable disassembly. Stable output — golden tests depend on it."""
        header = (f"{self.name}/{self.arity} "
                  f"(locals={self.n_locals}, globals={self.n_globals}):")
        lines = [header]
        for i, instr in enumerate(self.instrs):
            arg = instr.arg
            if arg is None:
                argstr = ""
            elif isinstance(arg, tuple):
                argstr = ", ".join(str(a) for a in arg)
            else:
                argstr = str(arg)
            line = f"{i:>4}  {instr.op.name:<16}{argstr}"
            # annotate LOAD_CONST / MAKE_CLOSURE for readability
            if instr.op is Op.LOAD_CONST and isinstance(arg, int) and 0 <= arg < len(self.consts):
                line = f"{line:<32}; {self.consts[arg]!r}"
            elif instr.op is Op.MAKE_CLOSURE and isinstance(arg, tuple):
                line = f"{line:<32}; <fn {self.consts[arg[0]].name}>"
            lines.append(line.rstrip())
        body = "\n".join(lines)
        nested = [c.dis() for c in self.consts if isinstance(c, Code)]   # function bodies
        return "\n\n".join([body, *nested])
