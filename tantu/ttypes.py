"""Tantu's type representation (DESIGN.md §3.3).

Named `ttypes` (not `types`) to avoid shadowing the stdlib `types` module, which
`dataclasses` itself imports. Frozen dataclasses; structural equality is type equality.

The full v1 set is defined now; M1's checker only ever produces `INT` (a knowing stub,
DESIGN.md M1) because types are erased before the VM.
"""

from __future__ import annotations

from dataclasses import dataclass


class Type:
    """Base for all Tantu types. Concrete types are frozen dataclasses below."""


@dataclass(frozen=True, slots=True)
class Prim(Type):
    name: str  # "Int" | "Float" | "Bool" | "Str" | "Unit"

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ListT(Type):
    elem: Type

    def __repr__(self) -> str:
        return f"List[{self.elem!r}]"


@dataclass(frozen=True, slots=True)
class TupleT(Type):
    items: tuple[Type, ...]

    def __repr__(self) -> str:
        return "(" + ", ".join(repr(t) for t in self.items) + ")"


@dataclass(frozen=True, slots=True)
class FnT(Type):
    params: tuple[Type, ...]
    ret: Type

    def __repr__(self) -> str:
        return "(" + ", ".join(repr(t) for t in self.params) + f") -> {self.ret!r}"


@dataclass(frozen=True, slots=True)
class ChanT(Type):
    elem: Type

    def __repr__(self) -> str:
        return f"Chan[{self.elem!r}]"


@dataclass(frozen=True, slots=True)
class EnumT(Type):
    name: str
    args: tuple[Type, ...] = ()  # e.g. Option[Int] => EnumT("Option", (INT,))

    def __repr__(self) -> str:
        if self.args:
            return f"{self.name}[" + ", ".join(repr(a) for a in self.args) + "]"
        return self.name


@dataclass(frozen=True, slots=True)
class StructT(Type):
    name: str
    args: tuple[Type, ...] = ()  # e.g. Pair[Int, Str] => StructT("Pair", (INT, STR))

    def __repr__(self) -> str:
        if self.args:
            return f"{self.name}[" + ", ".join(repr(a) for a in self.args) + "]"
        return self.name


@dataclass(frozen=True, slots=True)
class TypeVar(Type):
    name: str  # ONLY inside builtin signatures (DESIGN.md §3.2)

    def __repr__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class ErrorType(Type):
    """The type of an expression that failed to check. Absorbs further errors via the
    checker's `require`/`compatible` chokepoints (DESIGN.md §3.1, Fable ruling) — its
    equality stays identity-based so type hashing/sets are unaffected."""

    def __repr__(self) -> str:
        return "<error>"


@dataclass(frozen=True, slots=True)
class VariantDef(Type):
    name: str
    tag: int                 # 0-based declaration order
    payload: tuple           # tuple[Type, ...]; may contain TypeVar for prelude generics


@dataclass(frozen=True, slots=True)
class EnumDef(Type):
    name: str
    type_params: tuple       # tuple[str, ...]; () for monomorphic user enums
    variants: tuple          # tuple[VariantDef, ...]

    def variant(self, name: str) -> "VariantDef | None":
        for v in self.variants:
            if v.name == name:
                return v
        return None


@dataclass(frozen=True, slots=True)
class StructDef(Type):
    name: str
    type_params: tuple       # tuple[str, ...]; () for monomorphic user structs
    fields: tuple            # tuple[(field_name: str, Type), ...] in declaration order

    def field_index(self, name: str) -> int:
        for i, (fn, _) in enumerate(self.fields):
            if fn == name:
                return i
        return -1

    def field_type(self, name: str):
        for fn, ft in self.fields:
            if fn == name:
                return ft
        return None


# Canonical primitive singletons.
INT = Prim("Int")
FLOAT = Prim("Float")
BOOL = Prim("Bool")
STR = Prim("Str")
UNIT = Prim("Unit")
ERROR = ErrorType()


def subst(ty: Type, mapping: dict[str, Type]) -> Type:
    """Substitute TypeVars (by name) throughout a type — used by the mini-unifier & patterns."""
    if isinstance(ty, TypeVar):
        return mapping.get(ty.name, ty)
    if isinstance(ty, ListT):
        return ListT(subst(ty.elem, mapping))
    if isinstance(ty, ChanT):
        return ChanT(subst(ty.elem, mapping))
    if isinstance(ty, TupleT):
        return TupleT(tuple(subst(t, mapping) for t in ty.items))
    if isinstance(ty, FnT):
        return FnT(tuple(subst(t, mapping) for t in ty.params), subst(ty.ret, mapping))
    if isinstance(ty, EnumT):
        return EnumT(ty.name, tuple(subst(t, mapping) for t in ty.args))
    if isinstance(ty, StructT):
        return StructT(ty.name, tuple(subst(t, mapping) for t in ty.args))
    return ty
