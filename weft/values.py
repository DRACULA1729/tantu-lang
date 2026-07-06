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


def render(v: object, nested: bool = False) -> str:
    """Render a Weft value to its display string (Appendix B)."""
    # bool must precede int (bool is a subclass of int in Python)
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "()"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return f'"{_escape(v)}"' if nested else v
    if isinstance(v, list):
        return "[" + ", ".join(render(x, nested=True) for x in v) + "]"
    if isinstance(v, tuple):
        return "(" + ", ".join(render(x, nested=True) for x in v) + ")"
    if isinstance(v, Variant):
        if v.fields:
            return f"{v.variant_name}(" + ", ".join(render(x, nested=True) for x in v.fields) + ")"
        return v.variant_name
    if isinstance(v, Record):
        return f"{v.struct_name}(" + ", ".join(render(x, nested=True) for x in v.fields) + ")"
    if isinstance(v, (Closure, Builtin)):
        return f"<fn {getattr(v, 'name', getattr(getattr(v, 'code', None), 'name', '?'))}>"
    if isinstance(v, Channel):
        return "<chan>"
    return str(v)  # unreachable for well-typed programs
