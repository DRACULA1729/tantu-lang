"""The prelude registry: builtin generic enums (and, from M5, the builtin function table).

Shared by the checker (signatures) and — via the annotated Module — the compiler, so the two
can never disagree (DESIGN.md §3.3, §6.3). Named `prelude` (not `builtins`) to avoid shadowing
the stdlib module.
"""

from __future__ import annotations

from weft.wtypes import EnumDef, VariantDef, TypeVar

# Option[T] = Some(T) | None ; Result[T, E] = Ok(T) | Err(E).  Tags = declaration order.
_T = TypeVar("T")
_E = TypeVar("E")

OPTION = EnumDef("Option", ("T",), (VariantDef("Some", 0, (_T,)), VariantDef("None", 1, ())))
RESULT = EnumDef("Result", ("T", "E"), (VariantDef("Ok", 0, (_T,)), VariantDef("Err", 1, (_E,))))


def prelude_enums() -> dict[str, EnumDef]:
    """A fresh registry seeded with the prelude enums (user enums are added on top)."""
    return {"Option": OPTION, "Result": RESULT}


# --------------------------------------------------------------- builtin functions (M5, App. A)

import math

from weft.wtypes import FnT, ListT, EnumT, INT, FLOAT, STR
from weft.values import Variant


def _opt(name: str, elem):
    return EnumT("Option", (elem,))


# Builtin type signatures (checker): name -> list of FnT tried in order (first that unifies wins).
BUILTIN_SIGS: dict[str, list[FnT]] = {
    "str":       [FnT((_T,), STR)],
    "len":       [FnT((ListT(_T),), INT), FnT((STR,), INT)],
    "push":      [FnT((ListT(_T), _T), ListT(_T))],
    "get":       [FnT((ListT(_T), INT), EnumT("Option", (_T,)))],
    "concat":    [FnT((ListT(_T), ListT(_T)), ListT(_T))],
    "range":     [FnT((INT, INT), ListT(INT))],
    "float":     [FnT((INT,), FLOAT)],
    "trunc":     [FnT((FLOAT,), INT)],
    "parse_int": [FnT((STR,), EnumT("Option", (INT,)))],
    "split":     [FnT((STR, STR), ListT(STR))],
    "substring": [FnT((STR, INT, INT), STR)],
    "chars":     [FnT((STR,), ListT(STR))],
}


def _some(x):
    return Variant(0, "Option", "Some", (x,))


_NONE = Variant(1, "Option", "None", ())


def _get(xs, i):
    return _some(xs[i]) if 0 <= i < len(xs) else _NONE


def _parse_int(s):
    try:
        return _some(int(s))
    except ValueError:
        return _NONE


def _substring(s, i, j):
    n = len(s)
    i = max(0, min(i, n))
    j = max(i, min(j, n))
    return s[i:j]


def builtin_impls(render) -> dict:
    """Runtime implementations, keyed by name (the VM's builtin table). `render` is injected to
    avoid an import cycle with values."""
    return {
        "str":       lambda v: render(v),
        "len":       lambda x: len(x),
        "push":      lambda xs, x: xs + [x],          # returns a NEW list (immutable by convention)
        "get":       _get,
        "concat":    lambda a, b: a + b,
        "range":     lambda a, b: list(range(a, b)),
        "float":     lambda n: float(n),
        "trunc":     lambda x: math.trunc(x),
        "parse_int": _parse_int,
        "split":     lambda s, sep: s.split(sep),
        "substring": _substring,
        "chars":     lambda s: list(s),
    }
