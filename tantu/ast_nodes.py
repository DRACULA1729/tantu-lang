"""AST node definitions (DESIGN.md §3.5, §2.11).

Named `ast_nodes` (not `ast`) so we never shadow the stdlib `ast` — the exact mistake in the
old `Interpreter/` attempt, which had `AST.py` sitting beside `import ast`.

Hierarchy is append-only-safe: `Node(span)` -> `Expr(Node)` / `Stmt(Node)`. Later milestones
ADD subclasses, never reshape these. `Expr.ty` is keyword-only-with-default so subclasses can
declare positional fields after it without the "non-default argument follows default" error;
the checker fills `ty` in place (nodes stay mutable — NOT frozen).

Operators are small enums (never stringly-typed). `pretty()` emits a compact, tag-headed
s-expression that the parser tests golden against; its format is locked from M1.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from tantu.errors import Span
from tantu.ttypes import Type


class BinOpKind(enum.Enum):
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"
    EQ = "=="
    NE = "!="
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="

    @property
    def symbol(self) -> str:
        return self.value


class UnOpKind(enum.Enum):
    NEG = "-"
    NOT = "not"

    @property
    def symbol(self) -> str:
        return self.value


@dataclass
class Node:
    span: Span

    def pretty(self) -> str:
        raise NotImplementedError(type(self).__name__)


@dataclass
class Expr(Node):
    ty: Type | None = field(default=None, kw_only=True)


@dataclass
class Stmt(Node):
    pass


# --------------------------------------------------------------------------- expressions

@dataclass
class IntLit(Expr):
    value: int

    def pretty(self) -> str:
        return f"(int {self.value})"


@dataclass
class FloatLit(Expr):
    value: float

    def pretty(self) -> str:
        return f"(float {self.value})"


@dataclass
class StrLit(Expr):
    value: str

    def pretty(self) -> str:
        return f"(str {self.value!r})"


@dataclass
class BoolLit(Expr):
    value: bool

    def pretty(self) -> str:
        return f"(bool {'true' if self.value else 'false'})"


@dataclass
class UnitLit(Expr):
    """The unit literal `()` (§2.11 primary) — type Unit, runtime value None."""

    def pretty(self) -> str:
        return "(unit)"


@dataclass
class Name(Expr):
    ident: str
    res: object | None = field(default=None, kw_only=True)  # Resolution, filled by checker (M2+)

    def pretty(self) -> str:
        return f"(name {self.ident})"


@dataclass
class Unary(Expr):
    op: UnOpKind
    operand: Expr

    def pretty(self) -> str:
        return f"(unary {self.op.symbol} {self.operand.pretty()})"


@dataclass
class BinOp(Expr):
    op: BinOpKind
    left: Expr
    right: Expr

    def pretty(self) -> str:
        return f"(binop {self.op.symbol} {self.left.pretty()} {self.right.pretty()})"


@dataclass
class Call(Expr):
    func: Expr
    args: list[Expr]

    def pretty(self) -> str:
        parts = " ".join(a.pretty() for a in self.args)
        return f"(call {self.func.pretty()}{(' ' + parts) if parts else ''})"


# --------------------------------------------------------------------------- statements

@dataclass
class ExprStmt(Stmt):
    expr: Expr

    def pretty(self) -> str:
        return f"(expr-stmt {self.expr.pretty()})"


# --------------------------------------------------------------------------- module

@dataclass
class Module(Node):
    decls: list[Stmt]
    n_globals: int = field(default=0, kw_only=True)   # set by the checker (module root slot count)
    main_slot: int | None = field(default=None, kw_only=True)  # global slot of main()->Unit, if any
    symbols: dict = field(default_factory=dict, kw_only=True)   # exported top-level bindings (M8)

    def pretty(self) -> str:
        if not self.decls:
            return "(module)"
        return "(module " + " ".join(d.pretty() for d in self.decls) + ")"


# =========================================================================== M2

class LogicalKind(enum.Enum):
    AND = "and"
    OR = "or"

    @property
    def symbol(self) -> str:
        return self.value


def _block_pretty(stmts: list["Stmt"]) -> str:
    return "(block " + " ".join(s.pretty() for s in stmts) + ")" if stmts else "(block)"


# -- type expressions (parsed annotations; the checker resolves these to ttypes.Type) ------

@dataclass
class TypeExpr(Node):
    def pretty(self) -> str:
        raise NotImplementedError(type(self).__name__)


@dataclass
class NamedType(TypeExpr):
    name: str
    args: list["TypeExpr"]

    def pretty(self) -> str:
        if self.args:
            return f"(type {self.name} " + " ".join(a.pretty() for a in self.args) + ")"
        return f"(type {self.name})"


# -- expressions ---------------------------------------------------------------------------

@dataclass
class Logical(Expr):
    op: LogicalKind
    left: Expr
    right: Expr

    def pretty(self) -> str:
        return f"(logical {self.op.symbol} {self.left.pretty()} {self.right.pretty()})"


@dataclass
class IfExpr(Expr):
    branches: list  # list[tuple[Expr, list[Stmt]]] — (condition, body; value = tail expr)
    orelse: list    # list[Stmt] — an inline branch is a 1-element [ExprStmt(expr)]

    def pretty(self) -> str:
        arms = " ".join(f"({c.pretty()} {_block_pretty(b)})" for c, b in self.branches)
        return f"(if-expr {arms} (else {_block_pretty(self.orelse)}))"


# -- statements ----------------------------------------------------------------------------

@dataclass
class Let(Stmt):
    name: str
    annotation: TypeExpr | None
    value: Expr
    mutable: bool
    res: object | None = field(default=None, kw_only=True)  # Resolution, filled by checker

    def pretty(self) -> str:
        kw = "var" if self.mutable else "let"
        ann = f" {self.annotation.pretty()}" if self.annotation is not None else ""
        return f"({kw} {self.name}{ann} {self.value.pretty()})"


@dataclass
class Assign(Stmt):
    name: str
    value: Expr
    res: object | None = field(default=None, kw_only=True)

    def pretty(self) -> str:
        return f"(assign {self.name} {self.value.pretty()})"


@dataclass
class If(Stmt):
    branches: list        # list[tuple[Expr, list[Stmt]]]
    orelse: list | None   # list[Stmt] | None

    def pretty(self) -> str:
        arms = " ".join(f"({c.pretty()} {_block_pretty(b)})" for c, b in self.branches)
        els = f" (else {_block_pretty(self.orelse)})" if self.orelse is not None else ""
        return f"(if {arms}{els})"


@dataclass
class While(Stmt):
    cond: Expr
    body: list  # list[Stmt]

    def pretty(self) -> str:
        return f"(while {self.cond.pretty()} {_block_pretty(self.body)})"


@dataclass
class Loop(Stmt):
    body: list  # list[Stmt]

    def pretty(self) -> str:
        return f"(loop {_block_pretty(self.body)})"


@dataclass
class Break(Stmt):
    def pretty(self) -> str:
        return "(break)"


@dataclass
class Continue(Stmt):
    def pretty(self) -> str:
        return "(continue)"


# =========================================================================== M3

@dataclass
class Param(Node):
    name: str
    annotation: "TypeExpr"

    def pretty(self) -> str:
        return f"({self.name} {self.annotation.pretty()})"


def _params_pretty(params: list["Param"]) -> str:
    return "(params" + "".join(" " + p.pretty() for p in params) + ")"


@dataclass
class FnTypeExpr(TypeExpr):
    params: list        # list[TypeExpr]
    ret: "TypeExpr"

    def pretty(self) -> str:
        ps = "(params" + "".join(" " + p.pretty() for p in self.params) + ")"
        return f"(type-fn {ps} {self.ret.pretty()})"


def _tparams_pretty(type_params: list) -> str:
    """A ` (tparams T U)` segment, or empty when the declaration is monomorphic (M9b)."""
    return f" (tparams {' '.join(type_params)})" if type_params else ""


@dataclass
class FnDecl(Stmt):
    name: str
    params: list             # list[Param]
    ret: TypeExpr | None
    body: list               # list[Stmt]
    type_params: list = field(default_factory=list, kw_only=True)   # M9b: declared [T, ...]
    res: object | None = field(default=None, kw_only=True)          # Global slot (checker)
    n_locals: int = field(default=0, kw_only=True)                  # frame size (checker)
    captures: list = field(default_factory=list, kw_only=True)      # enclosing Resolutions
    fn_ty: object | None = field(default=None, kw_only=True)        # FnT (checker)

    def pretty(self) -> str:
        rp = f" (ret {self.ret.pretty()})" if self.ret is not None else ""
        tp = _tparams_pretty(self.type_params)
        return f"(fn {self.name}{tp} {_params_pretty(self.params)}{rp} {_block_pretty(self.body)})"


@dataclass
class Lambda(Expr):
    params: list             # list[Param]
    ret: TypeExpr | None
    body: list               # list[Stmt]
    n_locals: int = field(default=0, kw_only=True)
    captures: list = field(default_factory=list, kw_only=True)

    def pretty(self) -> str:
        rp = f" (ret {self.ret.pretty()})" if self.ret is not None else ""
        return f"(lambda {_params_pretty(self.params)}{rp} {_block_pretty(self.body)})"


@dataclass
class Return(Stmt):
    value: Expr | None

    def pretty(self) -> str:
        return f"(return {self.value.pretty()})" if self.value is not None else "(return)"


@dataclass
class Try(Expr):          # M6: postfix `?` — unwrap Ok/Some, else early-return Err/None
    operand: Expr

    def pretty(self) -> str:
        return f"(try {self.operand.pretty()})"


# =========================================================================== M7

@dataclass
class ChanNew(Expr):      # chan[T](cap?)
    elem_type: TypeExpr
    cap: Expr | None

    def pretty(self) -> str:
        c = self.cap.pretty() if self.cap is not None else "0"
        return f"(chan-new {self.elem_type.pretty()} {c})"


@dataclass
class Spawn(Stmt):        # spawn <call> | spawn <lambda>
    target: Expr

    def pretty(self) -> str:
        return f"(spawn {self.target.pretty()})"


# =========================================================================== M8

@dataclass
class Import(Stmt):       # import name  (loads sibling name.tn)
    name: str

    def pretty(self) -> str:
        return f"(import {self.name})"


# =========================================================================== M9a

@dataclass
class SelectArm(Node):
    kind: str            # "recv" | "send" | "else"
    binder: str | None   # recv only; None = discard (`_` normalized to None)
    chan: object | None  # Expr; recv/send only, None for else
    value: object | None # Expr; send only
    body: list           # list[Stmt]
    binder_res: object | None = field(default=None, kw_only=True)  # checker: Resolution of binder

    def pretty(self) -> str:
        if self.kind == "else":
            return f"(else-arm {_block_pretty(self.body)})"
        if self.kind == "recv":
            b = self.binder if self.binder is not None else "_"
            return f"(recv-arm {b} {self.chan.pretty()} {_block_pretty(self.body)})"
        return f"(send-arm {self.chan.pretty()} {self.value.pretty()} {_block_pretty(self.body)})"


@dataclass
class Select(Expr):
    arms: list           # list[SelectArm]
    stmt_position: bool = field(default=False, kw_only=True)   # checker: discarded (no arm agreement)

    def pretty(self) -> str:
        return "(select " + " ".join(a.pretty() for a in self.arms) + ")"


# =========================================================================== M4

@dataclass
class VariantDecl(Node):
    name: str
    payload: list        # list[TypeExpr]

    def pretty(self) -> str:
        if self.payload:
            return f"(variant {self.name} " + " ".join(t.pretty() for t in self.payload) + ")"
        return f"(variant {self.name})"


@dataclass
class EnumDecl(Stmt):
    name: str
    variants: list       # list[VariantDecl]
    type_params: list = field(default_factory=list, kw_only=True)   # M9b: declared [T, ...]

    def pretty(self) -> str:
        tp = _tparams_pretty(self.type_params)
        return f"(enum {self.name}{tp} " + " ".join(v.pretty() for v in self.variants) + ")"


# -- patterns ------------------------------------------------------------------------------

@dataclass
class Pattern(Node):
    def pretty(self) -> str:
        raise NotImplementedError(type(self).__name__)


@dataclass
class PWildcard(Pattern):
    def pretty(self) -> str:
        return "_"


@dataclass
class PLiteral(Pattern):
    value: object
    kind: str            # "int" | "str" | "bool"

    def pretty(self) -> str:
        return f"(lit {self.value!r})"


@dataclass
class PIdent(Pattern):
    name: str
    resolved: object | None = field(default=None, kw_only=True)  # checker: binding-slot or variant tag

    def pretty(self) -> str:
        return f"(pat {self.name})"


@dataclass
class PVariant(Pattern):
    name: str
    args: list           # list[Pattern]
    tag: int | None = field(default=None, kw_only=True)
    field_slots: list = field(default_factory=list, kw_only=True)  # checker: temp/binding slot per field

    def pretty(self) -> str:
        if self.args:
            return f"(variant {self.name} " + " ".join(a.pretty() for a in self.args) + ")"
        return f"(variant {self.name})"


@dataclass
class PTuple(Pattern):
    elems: list          # list[Pattern]
    field_slots: list = field(default_factory=list, kw_only=True)   # checker: temp/binding slot per element

    def pretty(self) -> str:
        return "(tuple-pat " + " ".join(e.pretty() for e in self.elems) + ")"


@dataclass
class MatchArm(Node):
    pattern: Pattern
    body: list           # list[Stmt]

    def pretty(self) -> str:
        return f"(arm {self.pattern.pretty()} {_block_pretty(self.body)})"


@dataclass
class Match(Expr):
    scrutinee: Expr
    arms: list           # list[MatchArm]
    scrut_slot: object | None = field(default=None, kw_only=True)   # checker: hidden scrutinee slot
    stmt_position: bool = field(default=False, kw_only=True)        # checker: discarded (no arm agreement)

    def pretty(self) -> str:
        return f"(match {self.scrutinee.pretty()} " + " ".join(a.pretty() for a in self.arms) + ")"


# =========================================================================== M5

@dataclass
class TupleTypeExpr(TypeExpr):
    items: list          # list[TypeExpr]

    def pretty(self) -> str:
        return "(type-tuple " + " ".join(t.pretty() for t in self.items) + ")"


@dataclass
class ListLit(Expr):
    elems: list          # list[Expr]

    def pretty(self) -> str:
        return "(list " + " ".join(e.pretty() for e in self.elems) + ")" if self.elems else "(list)"


@dataclass
class TupleLit(Expr):
    elems: list          # list[Expr], arity >= 2

    def pretty(self) -> str:
        return "(tuple " + " ".join(e.pretty() for e in self.elems) + ")"


@dataclass
class Index(Expr):
    obj: Expr
    index: Expr

    def pretty(self) -> str:
        return f"(index {self.obj.pretty()} {self.index.pretty()})"


@dataclass
class Field(Expr):
    obj: Expr
    name: str
    res: object | None = field(default=None, kw_only=True)   # checker: StructField | ImportedGlobal

    def pretty(self) -> str:
        return f"(field {self.obj.pretty()} {self.name})"


@dataclass
class FieldDecl(Node):
    name: str
    annotation: TypeExpr

    def pretty(self) -> str:
        return f"({self.name} {self.annotation.pretty()})"


@dataclass
class StructDecl(Stmt):
    name: str
    fields: list         # list[FieldDecl]
    type_params: list = field(default_factory=list, kw_only=True)   # M9b: declared [T, ...]

    def pretty(self) -> str:
        tp = _tparams_pretty(self.type_params)
        return f"(struct {self.name}{tp} " + " ".join(f.pretty() for f in self.fields) + ")"


@dataclass
class For(Stmt):
    var: str
    iterable: Expr
    body: list           # list[Stmt]
    var_slot: object | None = field(default=None, kw_only=True)     # checker: loop-var slot
    hidden: object | None = field(default=None, kw_only=True)       # checker: (xs, i, n) temp slots

    def pretty(self) -> str:
        return f"(for {self.var} {self.iterable.pretty()} {_block_pretty(self.body)})"


@dataclass
class LetTuple(Stmt):
    names: list          # list[str]
    annotation: TypeExpr | None
    value: Expr
    res_list: list = field(default_factory=list, kw_only=True)      # checker: Resolution per name
    tmp: object | None = field(default=None, kw_only=True)          # checker: hidden tuple temp

    def pretty(self) -> str:
        names = " ".join(self.names)
        return f"(let-tuple ({names}) {self.value.pretty()})"
