"""Runtime value types and value rendering (DESIGN.md §5.1, Appendix B).

Types are erased before the VM, so primitives are plain Python objects. The compound value
classes are defined now (they are just data) so later milestones can construct them without
touching this module's public surface; M1 only ever renders primitives.

`render(v, nested=False)` has two modes (Appendix B): top-level prints a Str bare (used by
`print`/`str`); nested prints it quoted. Rendering is locked so golden tests are stable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Variant:
    tag: int
    enum_name: str
    variant_name: str
    fields: tuple


@dataclass(frozen=True, slots=True)
class Record:
    struct_name: str
    fields: tuple


@dataclass(frozen=True, slots=True)
class Closure:
    code: object       # bytecode.Code (avoid import cycle: VM supplies it)
    captures: tuple


@dataclass(frozen=True, slots=True)
class Builtin:
    """A marker for a prelude builtin (M5). The name resolves to a Python impl in the VM's
    builtin table; carried in the constant pool and dispatched by CALL."""
    name: str
    arity: int      # -1 for variadic / overloaded (checker validates arity)


class Channel:
    """A typed CSP channel (fully wired at M7). Defined here so `render` can name it."""

    __slots__ = ("buf", "cap", "closed", "send_wait", "recv_wait")

    def __init__(self, cap: int = 0) -> None:
        from collections import deque
        self.buf = deque()
        self.cap = cap
        self.closed = False
        self.send_wait = deque()
        self.recv_wait = deque()


_STR_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _escape(s: str) -> str:
    return "".join(_STR_ESCAPES.get(ch, ch) for ch in s)


def _push_children(work: list, children, suffix: str) -> None:
    """Schedule `children` (comma-separated, nested) followed by `suffix` on the work stack.

    The stack pops LIFO, so items are pushed in reverse emission order: suffix first, then
    child_{n-1} .. child_0 with a ", " literal between each pair.
    """
    work.append(suffix)
    for i in range(len(children) - 1, -1, -1):
        work.append((children[i], True))
        if i:
            work.append(", ")


def render(v: object, nested: bool = False) -> str:
    """Render a Weft value to its display string (Appendix B).

    Iterative (explicit work stack) so deeply nested values — a 5000-deep cons list, say —
    render without hitting Python's recursion limit. Output is byte-identical to the original
    recursive form; the golden suite is the guardrail.

    Work-stack items are either a bare `str` (a literal token to emit) or a `(value, nested)`
    pair. Value pairs are always 2-tuples built here, so a Weft tuple value (itself a Python
    tuple) can never be mistaken for a literal — literals are checked by exact `str` type.
    """
    out: list[str] = []
    work: list = [(v, nested)]
    while work:
        item = work.pop()
        if type(item) is str:       # a pre-rendered literal token (separator/suffix)
            out.append(item)
            continue
        v, nested = item
        # bool must precede int (bool is a subclass of int in Python)
        if isinstance(v, bool):
            out.append("true" if v else "false")
        elif v is None:
            out.append("()")
        elif isinstance(v, int):
            out.append(str(v))
        elif isinstance(v, float):
            out.append(repr(v))
        elif isinstance(v, str):
            out.append(f'"{_escape(v)}"' if nested else v)
        elif isinstance(v, list):
            out.append("[")
            _push_children(work, v, "]")
        elif isinstance(v, tuple):
            out.append("(")
            _push_children(work, v, ")")
        elif isinstance(v, Variant):
            if v.fields:
                out.append(f"{v.variant_name}(")
                _push_children(work, v.fields, ")")
            else:
                out.append(v.variant_name)
        elif isinstance(v, Record):
            out.append(f"{v.struct_name}(")
            _push_children(work, v.fields, ")")
        elif isinstance(v, (Closure, Builtin)):
            out.append(f"<fn {getattr(v, 'name', getattr(getattr(v, 'code', None), 'name', '?'))}>")
        elif isinstance(v, Channel):
            out.append("<chan>")
        else:
            out.append(str(v))      # unreachable for well-typed programs
    return "".join(out)
