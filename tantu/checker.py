"""Type checker / semantic pass (DESIGN.md §3, §6.2).

M3 adds functions: two-pass module checking (hoist all top-level `fn` signatures, then check
bodies in source order — Model B), function frames (params + block-locals become Locals),
closure capture by value, the var-capture ban (a function may not touch any mutable binding
from an enclosing scope — including a module `var` — which is what keeps M7 fibers race-free),
and the block-as-value / missing-return (E0108) analysis.

Errors are collected; the `ERROR` sentinel is absorbed at `require`. Resolution lives in
resolve.py; `synth`/`stmt` grow here per milestone.
"""

from __future__ import annotations

from tantu import ast_nodes as A
from tantu import exhaustive
from tantu.resolve import (
    Scope, FrameRoot, ERROR_RES, resolve, Global, Local, VariantRes, StructRes, Builtin,
    StructField, ImportedGlobal,
)
from tantu.ttypes import (
    Type, FnT, EnumT, EnumDef, VariantDef, StructDef, StructT, TypeVar, ListT, ChanT, TupleT,
    INT, FLOAT, BOOL, STR, UNIT, ERROR, subst,
)
from tantu.prelude import prelude_enums, BUILTIN_SIGS
from tantu.errors import (
    CheckError, CheckErrors, Span,
    E_UNSUPPORTED, E_DUPLICATE, E_ASSIGN_IMMUTABLE, E_VAR_CAPTURE, E_NON_BOOL_COND,
    E_MISSING_RETURN, E_INTRINSIC_NOT_VALUE, E_UNKNOWN_NAME, E_TYPE_MISMATCH, E_NOT_COMPARABLE,
    E_LOOP_CONTROL, E_UNKNOWN_TYPE, E_RETURN_OUTSIDE, E_NOT_CALLABLE, E_ARITY,
    E_NON_EXHAUSTIVE, E_CANNOT_INFER, E_UNREACHABLE, E_NOT_A_VARIANT, E_VARIANT_ARITY,
    E_CONSTRUCTOR_NOT_VALUE, E_DUP_VARIANT, E_EMPTY_LIST, E_QUESTION,
    E_MODULE_NOT_FOUND, E_NOT_A_MODULE_MEMBER, E_MODULE_NOT_VALUE, E_IMPORT_UNSUPPORTED,
    E_SELECT_ELSE, E_GENERIC_NOT_VALUE,
)

# Return-frame sentinel for an unannotated inline lambda: its return type is inferred from the
# body expression, so `?`/`return` have no frame type to check against and must be rejected.
_INFER = object()

_PRIMS = {"Int": INT, "Float": FLOAT, "Bool": BOOL, "Str": STR, "Unit": UNIT}
_ARITH = {A.BinOpKind.ADD, A.BinOpKind.SUB, A.BinOpKind.MUL, A.BinOpKind.DIV, A.BinOpKind.MOD}
_EQUALITY = {A.BinOpKind.EQ, A.BinOpKind.NE}
_ORDERING = {A.BinOpKind.LT, A.BinOpKind.LE, A.BinOpKind.GT, A.BinOpKind.GE}
_RESERVED = {"print", "send", "recv", "close", "sleep", "chan"}   # unshadowable prelude names (§2.2)
_PRELUDE_NAMES = {"Some", "None", "Ok", "Err", "Option", "Result"}   # prelude ctor/type names (§2.2)

# Channel intrinsics (M7): call-position-only, compiled to dedicated opcodes (not CALL).
_ITV = TypeVar("T")
_INTRINSICS: dict[str, list] = {
    "send":  [FnT((ChanT(_ITV), _ITV), UNIT)],
    "recv":  [FnT((ChanT(_ITV),), EnumT("Option", (_ITV,)))],
    "close": [FnT((ChanT(_ITV),), UNIT)],
    "sleep": [FnT((INT,), UNIT)],
}


class _Checker:
    def __init__(self) -> None:
        self.errors: list[CheckError] = []
        self.loop_depth = 0
        # enclosing function return types (or _INFER for an unannotated inline lambda);
        # empty = module level
        self.ret_stack: list = []
        self.main_slot: int | None = None
        self.enums: dict[str, EnumDef] = prelude_enums()
        self.enum_names: set[str] = set()                     # user enum names (for forward refs)
        self.structs: dict[str, StructDef] = {}               # struct registry (M5)
        self.struct_names: set[str] = set()                   # user struct names (forward refs)
        self.fn_names: set[str] = set()                       # top-level fn names (§2.7 census)
        self.module_names: set[str] = set()                   # imported module names (M8)
        self.imports: dict[str, dict] = {}                    # module -> {sym: (slot, type)}
        self.variant_index: dict[str, tuple[str, int]] = {}   # variant name -> (enum, tag)
        self.type_params: dict[str, TypeVar] = {}             # in-scope generic params (M9b)
        self.enum_params: dict[str, tuple] = {}               # user enum name -> declared params
        self.struct_params: dict[str, tuple] = {}             # user struct name -> declared params
        for edef in self.enums.values():
            for v in edef.variants:
                self.variant_index[v.name] = (edef.name, v.tag)

    # -- temp-slot allocation (match scrutinee + payloads) -------------------

    def _temp(self, scope: Scope):
        slot = scope.root.alloc()
        return Global(slot) if scope.root.kind == "global" else Local(slot)

    def error(self, code: str, msg: str, span: Span, hint: str | None = None) -> None:
        self.errors.append(CheckError(code, msg, span, hint=hint))

    def require(self, actual: Type, expected: Type, span: Span) -> None:
        if actual is ERROR or expected is ERROR or actual == expected:
            return
        self.error(E_TYPE_MISMATCH, f"expected {expected!r}, found {actual!r}", span)

    # -- generics: type-param scope + instantiation (M9b) --------------------

    def _enter_type_params(self, names: list, span: Span, *, report: bool = True) -> dict:
        """Make `names` the active generic-param scope; return the prior scope to restore.

        A param may not repeat or collide with an existing type name (E0101). Type params are
        types (rigid TypeVars), never values — they get no binding slot (Fable ruling). `report`
        is False when re-entering an fn's params for its body (already validated at hoist time).
        """
        saved = self.type_params
        tps: dict[str, TypeVar] = {}
        for n in names:
            if report and n in tps:
                self.error(E_DUPLICATE, f"duplicate type parameter '{n}'", span)
            elif report and (n in _PRIMS or n in ("List", "Chan") or n in self.enums
                             or n in self.enum_names or n in self.structs or n in self.struct_names):
                self.error(E_DUPLICATE,
                           f"type parameter '{n}' conflicts with an existing type name", span)
            tps[n] = TypeVar(n)
        self.type_params = tps
        return saved

    def _tyvar_names(self, ty: Type) -> list[str]:
        """Ordered, de-duplicated TypeVar names occurring in `ty` (params left-to-right, then ret)."""
        out: list[str] = []

        def walk(t: Type) -> None:
            if isinstance(t, TypeVar):
                if t.name not in out:
                    out.append(t.name)
            elif isinstance(t, (ListT, ChanT)):
                walk(t.elem)
            elif isinstance(t, TupleT):
                for x in t.items:
                    walk(x)
            elif isinstance(t, (EnumT, StructT)):
                for x in t.args:
                    walk(x)
            elif isinstance(t, FnT):
                for x in t.params:
                    walk(x)
                walk(t.ret)

        walk(ty)
        return out

    def _has_typevar(self, ty: Type) -> bool:
        return bool(self._tyvar_names(ty))

    def _contains_error(self, ty: Type) -> bool:
        """True if the ERROR sentinel appears anywhere in `ty` — used to stop a failed
        sub-expression from leaking `<error>` into user-facing generic-call diagnostics."""
        if ty is ERROR:
            return True
        if isinstance(ty, (ListT, ChanT)):
            return self._contains_error(ty.elem)
        if isinstance(ty, TupleT):
            return any(self._contains_error(t) for t in ty.items)
        if isinstance(ty, FnT):
            return any(self._contains_error(t) for t in ty.params) or self._contains_error(ty.ret)
        if isinstance(ty, (EnumT, StructT)):
            return any(self._contains_error(t) for t in ty.args)
        return False

    def _subst_poisoned(self, sub: dict) -> bool:
        """True if any bound type in the substitution carries ERROR (inference already failed)."""
        return any(self._contains_error(t) for t in sub.values())

    def _generic_callee(self, e: A.Call, scope: Scope) -> tuple[object, FnT] | None:
        """If `e.func` names a generic fn (local binding or imported member), return (res, FnT)
        and set `e.func.res`. Otherwise None. Pure lookup — emits no errors (Fable ruling)."""
        f = e.func
        if isinstance(f, A.Name):
            info = scope.lookup(f.ident)
            if info is not None and info.generic and isinstance(info.ty, FnT):
                f.res = resolve(scope.root, info)
                return info.ty
        elif isinstance(f, A.Field) and isinstance(f.obj, A.Name) \
                and f.obj.ident in self.module_names and scope.lookup(f.obj.ident) is None:
            mod = self.imports.get(f.obj.ident, {})
            if f.name in mod:
                slot, ty, _ = mod[f.name]
                if isinstance(ty, FnT) and self._has_typevar(ty):
                    f.res = ImportedGlobal(f.obj.ident, slot)
                    return ty
        return None

    def _callee_label(self, e: A.Call) -> str:
        f = e.func
        if isinstance(f, A.Name):
            return f.ident
        if isinstance(f, A.Field) and isinstance(f.obj, A.Name):
            return f"{f.obj.ident}.{f.name}"
        return "<fn>"

    def _call_generic(self, e: A.Call, fty: FnT, scope: Scope, expected: Type | None,
                      label: str) -> Type:
        """Instantiate a generic fn call: unify declared params with args (then the expected type
        with the return), pin every occurring param, and return the substituted result type."""
        arg_tys = [self.synth(a, scope) for a in e.args]
        if len(e.args) != len(fty.params):
            self.error(E_ARITY,
                       f"expected {len(fty.params)} argument(s), got {len(e.args)}", e.span)
            return ERROR
        sub: dict = {}
        poisoned = False
        for pt, at, a in zip(fty.params, arg_tys, e.args):
            if self._contains_error(at):   # E0119 already reported at this arg; don't cascade
                poisoned = True
                continue
            if not self._unify(pt, at, sub):
                if poisoned or self._subst_poisoned(sub):   # inference already failed: stay quiet
                    poisoned = True
                else:
                    self.error(E_TYPE_MISMATCH,
                               f"expected {subst(pt, sub)!r}, found {at!r}", a.span)
        if poisoned or self._subst_poisoned(sub):
            return ERROR                   # suppress the expected-type unify and the infer loop
        if expected is not None:
            self._unify(fty.ret, expected, sub)
        for name in self._tyvar_names(fty):
            if name not in sub:
                self.error(E_CANNOT_INFER,
                           f"cannot infer type argument '{name}' of '{label}'; add an annotation",
                           e.span)
                sub[name] = ERROR
        e.func.ty = FnT(tuple(subst(p, sub) for p in fty.params), subst(fty.ret, sub))
        return subst(fty.ret, sub)

    # -- top-level: register enums, hoist fn signatures, then check ---------

    def register_enum(self, d: A.EnumDecl) -> None:
        if d.name in self.enums or d.name in _PRIMS:
            self.error(E_DUPLICATE, f"duplicate definition of type '{d.name}'", d.span)
        saved = self._enter_type_params(d.type_params, d.span)   # payloads may reference T (M9b)
        variants = []
        for i, v in enumerate(d.variants):
            payload = tuple(self._resolve_type(t) for t in v.payload)
            variants.append(VariantDef(v.name, i, payload))
        self.type_params = saved
        edef = EnumDef(d.name, tuple(d.type_params), tuple(variants))
        self.enums[d.name] = edef
        for v in edef.variants:
            if v.name in self.variant_index:
                self.error(E_DUP_VARIANT, f"duplicate variant name '{v.name}'", d.span)
            elif v.name in self.fn_names or v.name in self.struct_names:   # §2.7: one namespace
                self.error(E_DUPLICATE,
                           f"variant '{v.name}' conflicts with an existing function or struct",
                           d.span)
            self.variant_index[v.name] = (d.name, v.tag)

    def hoist_fn(self, d: A.FnDecl, scope: Scope) -> None:
        if d.name == "main" and d.type_params:
            self.error(E_UNSUPPORTED, "'main' cannot declare type parameters", d.span)
        saved = self._enter_type_params(d.type_params, d.span)   # signature may reference T (M9b)
        param_tys = tuple(self._resolve_type(p.annotation) for p in d.params)
        ret_ty = self._resolve_type(d.ret) if d.ret is not None else UNIT
        self.type_params = saved
        d.fn_ty = FnT(param_tys, ret_ty)
        if d.name in self.struct_names or d.name in self.enum_names:   # §2.7: one namespace
            self.error(E_DUPLICATE, f"'{d.name}' conflicts with an existing type", d.span)
        info = self._declare_checked(scope, d.name, d.fn_ty, False, d.span,
                                     generic=bool(d.type_params))
        d.res = info.resolution()
        if d.name == "main" and not d.type_params and d.fn_ty == FnT((), UNIT):
            self.main_slot = info.slot

    def register_struct(self, d: A.StructDecl) -> None:
        if d.name in self.structs or d.name in self.enums or d.name in _PRIMS \
                or d.name in self.enum_names:
            self.error(E_DUPLICATE, f"duplicate definition of type '{d.name}'", d.span)
        saved = self._enter_type_params(d.type_params, d.span)   # fields may reference T (M9b)
        seen: set[str] = set()
        for f in d.fields:
            if f.name in seen:
                self.error(E_DUPLICATE, f"duplicate field '{f.name}'", d.span)
            seen.add(f.name)
        fields = tuple((f.name, self._resolve_type(f.annotation)) for f in d.fields)
        self.type_params = saved
        self.structs[d.name] = StructDef(d.name, tuple(d.type_params), fields)

    def item(self, d: A.Stmt, scope: Scope) -> None:
        if isinstance(d, A.FnDecl):
            self._check_fn_decl(d, scope)
        elif isinstance(d, (A.EnumDecl, A.StructDecl, A.Import)):
            pass   # registered in the signature pass; no runtime code
        else:
            self.stmt(d, scope)

    def _check_fn_decl(self, d: A.FnDecl, scope: Scope) -> None:
        # T is in scope in the body (M9b); already validated at hoist time, so don't re-report
        saved_tps = self._enter_type_params(d.type_params, d.span, report=False)
        fn_scope = scope.child_frame("local")
        for p, pty in zip(d.params, d.fn_ty.params):
            self._declare_checked(fn_scope, p.name, pty, False, p.span)
        saved = self.loop_depth
        self.loop_depth = 0
        self.ret_stack.append(d.fn_ty.ret)
        self._check_fn_body(d.body, fn_scope, d.fn_ty.ret, d.span)
        self.ret_stack.pop()
        self.loop_depth = saved
        self.type_params = saved_tps
        d.n_locals = fn_scope.root.counter
        d.captures = fn_scope.root.captures

    def _check_fn_body(self, body: list[A.Stmt], scope: Scope, R: Type, span: Span) -> None:
        if R is ERROR or R == UNIT:
            for s in body:
                self.stmt(s, scope)
        elif not self._body_yields(body, scope, R):
            where = body[-1].span if body else span
            self.error(E_MISSING_RETURN,
                       f"missing return path — function must return {R!r} on all paths", where)

    # -- block-as-value / missing-return (Fable ruling 4) -------------------

    def _body_yields(self, stmts: list[A.Stmt], scope: Scope, R: Type) -> bool:
        if not stmts:
            return False
        for s in stmts[:-1]:
            self.stmt(s, scope)
        return self._tail_yields(stmts[-1], scope, R)

    def _tail_yields(self, s: A.Stmt, scope: Scope, R: Type) -> bool:
        if isinstance(s, A.Return):
            self.stmt(s, scope)
            return True
        if isinstance(s, A.ExprStmt):
            self.check_expr(s.expr, R, scope)
            return True
        if isinstance(s, A.If) and s.orelse is not None:
            ok = True
            for cond, body in s.branches:
                self._condition(cond, scope)
                ok = self._body_yields(body, scope.child(), R) and ok
            ok = self._body_yields(s.orelse, scope.child(), R) and ok
            return ok
        self.stmt(s, scope)   # checked for effect; does not yield
        return False

    # -- statements ----------------------------------------------------------

    def stmt(self, s: A.Stmt, scope: Scope) -> None:
        if isinstance(s, A.Let):
            self._let(s, scope)
        elif isinstance(s, A.Assign):
            self._assign(s, scope)
        elif isinstance(s, A.ExprStmt):
            if isinstance(s.expr, A.Match):
                self._check_match(s.expr, scope, expected=None, stmt_position=True)
            elif isinstance(s.expr, A.Select):
                self._check_select(s.expr, scope, expected=None, stmt_position=True)
            else:
                self.synth(s.expr, scope)
        elif isinstance(s, A.If):
            self._if_stmt(s, scope)
        elif isinstance(s, A.While):
            self._condition(s.cond, scope)
            self._loop_body(s.body, scope)
        elif isinstance(s, A.Loop):
            self._loop_body(s.body, scope)
        elif isinstance(s, A.Break):
            self._loop_control(s, "break")
        elif isinstance(s, A.Continue):
            self._loop_control(s, "continue")
        elif isinstance(s, A.Return):
            self._return(s, scope)
        elif isinstance(s, A.For):
            self._for(s, scope)
        elif isinstance(s, A.LetTuple):
            self._check_let_tuple(s, scope)
        elif isinstance(s, A.Spawn):
            self._spawn(s, scope)
        else:  # pragma: no cover
            raise AssertionError(f"checker: unexpected statement {type(s).__name__}")

    def _declare_checked(self, scope: Scope, name: str, ty: Type, mutable: bool, span: Span,
                         *, generic: bool = False):
        """Declare a value binding, enforcing name hygiene (§2.2/§2.7): prelude/reserved names
        are unshadowable, imported module names may not be re-bound, and a name may not be
        declared twice in the same scope (REPL module scope excepted)."""
        if name in _RESERVED or name in _PRELUDE_NAMES:
            self.error(E_INTRINSIC_NOT_VALUE, f"cannot rebind prelude name '{name}'", span)
        if name in self.module_names:
            self.error(E_DUPLICATE, f"'{name}' conflicts with imported module '{name}'", span)
        prior = scope.local(name)
        if prior is not None and not scope.repl:
            self.error(E_DUPLICATE, f"duplicate definition of '{name}'", span,
                       hint=f"previously defined at line {prior.decl_span.line}")
        return scope.declare(name, ty, mutable, span, generic=generic)

    def _let(self, s: A.Let, scope: Scope) -> None:
        if s.annotation is not None:
            declared = self._resolve_type(s.annotation)
            self.check_expr(s.value, declared, scope)
            ty = declared
        else:
            ty = self.synth(s.value, scope)
        s.res = self._declare_checked(scope, s.name, ty, s.mutable, s.span).resolution()

    def _assign(self, s: A.Assign, scope: Scope) -> None:
        info = scope.lookup(s.name)
        if info is None:
            self.error(E_UNKNOWN_NAME, f"unknown name '{s.name}'", s.span)
            self.synth(s.value, scope)
            s.res = ERROR_RES
            return
        if not info.mutable:
            self.error(E_ASSIGN_IMMUTABLE, f"cannot assign to immutable binding '{s.name}'",
                       s.span, hint=f"'{s.name}' is a 'let' (declared at line "
                                    f"{info.decl_span.line}); use 'var' to allow reassignment")
        elif scope.root.kind == "local" and info.root is not scope.root:
            self.error(E_VAR_CAPTURE,
                       f"cannot assign to mutable binding '{s.name}' from inside a function",
                       s.span)
        self.check_expr(s.value, info.ty, scope)
        s.res = resolve(scope.root, info)

    def _if_stmt(self, s: A.If, scope: Scope) -> None:
        for cond, body in s.branches:
            self._condition(cond, scope)
            self._block(body, scope)
        if s.orelse is not None:
            self._block(s.orelse, scope)

    def _loop_body(self, body: list[A.Stmt], scope: Scope) -> None:
        self.loop_depth += 1
        self._block(body, scope)
        self.loop_depth -= 1

    def _loop_control(self, s: A.Stmt, kw: str) -> None:
        if self.loop_depth == 0:
            self.error(E_LOOP_CONTROL, f"'{kw}' outside a loop", s.span)

    def _return(self, s: A.Return, scope: Scope) -> None:
        if not self.ret_stack:
            self.error(E_RETURN_OUTSIDE, "'return' outside a function", s.span)
            if s.value is not None:
                self.synth(s.value, scope)
            return
        R = self.ret_stack[-1]
        if R is _INFER:
            self.error(E_QUESTION,
                       "'return' inside a lambda without a return-type annotation; add '-> T'",
                       s.span)
            if s.value is not None:
                self.synth(s.value, scope)
            return
        if s.value is not None:
            self.check_expr(s.value, R, scope)
        elif R is not ERROR and R != UNIT:
            self.error(E_TYPE_MISMATCH, f"bare 'return' but function returns {R!r}", s.span)

    def _block(self, stmts: list[A.Stmt], scope: Scope) -> None:
        child = scope.child()
        for st in stmts:
            self.stmt(st, child)

    def _condition(self, expr: A.Expr, scope: Scope) -> None:
        t = self.synth(expr, scope)
        if t is not ERROR and t != BOOL:
            self.error(E_NON_BOOL_COND, "condition must be Bool", expr.span)

    # -- expressions ---------------------------------------------------------

    def check_expr(self, expr: A.Expr, expected: Type, scope: Scope) -> None:
        # bidirectional: flow `expected` into variant construction so None/Err can pin type args
        if self._is_variant_call(expr, scope):
            self.require(self._construct_variant(expr, scope, expected), expected, expr.span)
            return
        if isinstance(expr, A.Call):           # flow `expected` into generic calls / struct ctors
            gen = self._generic_callee(expr, scope)
            if gen is not None:
                self.require(self._call_generic(expr, gen, scope, expected,
                             self._callee_label(expr)), expected, expr.span)
                return
            if isinstance(expr.func, A.Name) and scope.lookup(expr.func.ident) is None \
                    and expr.func.ident in self.structs and self.structs[expr.func.ident].type_params:
                self.require(self._construct_struct(expr, scope, expected), expected, expr.span)
                return
        if self._is_variant_name(expr, scope):
            enum_name, tag = self.variant_index[expr.ident]
            if len(self.enums[enum_name].variants[tag].payload) == 0:
                self.require(self._nullary_variant(expr, scope, expected), expected, expr.span)
                return
        if isinstance(expr, A.ListLit) and isinstance(expected, ListT):
            if expr.elems:                     # flow the element type into every element
                for x in expr.elems:
                    self.check_expr(x, expected.elem, scope)
            expr.ty = expected                 # empty list gets its element type from context
            return
        if isinstance(expr, A.TupleLit) and isinstance(expected, TupleT) \
                and len(expr.elems) == len(expected.items):
            for x, ety in zip(expr.elems, expected.items):
                self.check_expr(x, ety, scope)
            expr.ty = expected
            return
        if isinstance(expr, A.IfExpr):         # flow `expected` into each branch (inference)
            self._if_expr(expr, scope, expected)
            return
        if isinstance(expr, A.Match):
            self._check_match(expr, scope, expected=expected, stmt_position=False)
            return
        if isinstance(expr, A.Select):
            self._check_select(expr, scope, expected=expected, stmt_position=False)
            return
        self.require(self.synth(expr, scope), expected, expr.span)

    def synth(self, expr: A.Expr, scope: Scope) -> Type:
        t = self._synth(expr, scope)
        expr.ty = t
        return t

    def _synth(self, expr: A.Expr, scope: Scope) -> Type:
        if isinstance(expr, A.IntLit):
            return INT
        if isinstance(expr, A.FloatLit):
            return FLOAT
        if isinstance(expr, A.BoolLit):
            return BOOL
        if isinstance(expr, A.StrLit):
            return STR
        if isinstance(expr, A.UnitLit):
            return UNIT
        if isinstance(expr, A.Name):
            return self._name(expr, scope)
        if isinstance(expr, A.Unary):
            return self._unary(expr, scope)
        if isinstance(expr, A.BinOp):
            return self._binop(expr, scope)
        if isinstance(expr, A.Logical):
            return self._logical(expr, scope)
        if isinstance(expr, A.IfExpr):
            return self._if_expr(expr, scope)
        if isinstance(expr, A.Lambda):
            return self._lambda(expr, scope)
        if isinstance(expr, A.Match):
            return self._check_match(expr, scope, expected=None, stmt_position=False)
        if isinstance(expr, A.Select):
            return self._check_select(expr, scope, expected=None, stmt_position=False)
        if isinstance(expr, A.ListLit):
            return self._list_lit(expr, scope)
        if isinstance(expr, A.TupleLit):
            return TupleT(tuple(self.synth(x, scope) for x in expr.elems))
        if isinstance(expr, A.Index):
            return self._index(expr, scope)
        if isinstance(expr, A.Field):
            return self._field(expr, scope)
        if isinstance(expr, A.Try):
            return self._try(expr, scope)
        if isinstance(expr, A.ChanNew):
            elem = self._resolve_type(expr.elem_type)
            if expr.cap is not None:
                self.check_expr(expr.cap, INT, scope)
            return ChanT(elem)
        if isinstance(expr, A.Call):
            return self._call(expr, scope)
        return ERROR  # pragma: no cover

    def _name(self, e: A.Name, scope: Scope) -> Type:
        info = scope.lookup(e.ident)
        if info is None:
            if e.ident in self.variant_index:               # a variant used as a value
                enum_name, tag = self.variant_index[e.ident]
                if len(self.enums[enum_name].variants[tag].payload) == 0:
                    return self._nullary_variant(e, scope, None)
                self.error(E_CONSTRUCTOR_NOT_VALUE,
                           f"variant constructor '{e.ident}' is not a value; "
                           f"wrap it: fn(x): {e.ident}(x)", e.span)
                e.res = ERROR_RES
                return ERROR
            if e.ident in self.structs:
                self.error(E_CONSTRUCTOR_NOT_VALUE,
                           f"struct constructor '{e.ident}' is not a value", e.span)
                e.res = ERROR_RES
                return ERROR
            if e.ident in self.module_names:
                self.error(E_MODULE_NOT_VALUE, f"module '{e.ident}' is not a value", e.span)
                e.res = ERROR_RES
                return ERROR
            if e.ident in BUILTIN_SIGS:
                self.error(E_INTRINSIC_NOT_VALUE,
                           f"builtin '{e.ident}' is not a first-class value", e.span)
                e.res = ERROR_RES
                return ERROR
            if e.ident in _RESERVED:
                self.error(E_INTRINSIC_NOT_VALUE,
                           f"intrinsic '{e.ident}' is not a first-class value", e.span)
            else:
                self.error(E_UNKNOWN_NAME, f"unknown name '{e.ident}'", e.span)
            e.res = ERROR_RES
            return ERROR
        # var-capture ban: inside a function, no reference to an outer mutable binding
        if scope.root.kind == "local" and info.root is not scope.root and info.mutable:
            if info.root.kind == "global":
                self.error(E_VAR_CAPTURE, f"cannot access mutable binding '{e.ident}' from "
                           f"inside a function; copy it into a 'let' or pass it as a parameter",
                           e.span)
            else:
                self.error(E_VAR_CAPTURE,
                           f"cannot capture mutable binding '{e.ident}'; copy it into a 'let' first",
                           e.span)
            e.res = ERROR_RES
            return info.ty
        if info.generic:                          # a generic fn is call-only, never a value (M9b)
            self.error(E_GENERIC_NOT_VALUE,
                       f"generic function '{e.ident}' is not a first-class value", e.span,
                       hint="call it directly — type arguments are always inferred — or wrap it "
                            "in a lambda with concrete parameter types")
            e.res = ERROR_RES
            return ERROR
        e.res = resolve(scope.root, info)
        return info.ty

    def _unary(self, e: A.Unary, scope: Scope) -> Type:
        t = self.synth(e.operand, scope)
        if t is ERROR:
            return ERROR
        if e.op is A.UnOpKind.NEG:
            if t == INT or t == FLOAT:
                return t
            self.error(E_TYPE_MISMATCH, f"unary '-' expects Int or Float, found {t!r}", e.span)
            return ERROR
        if t == BOOL:
            return BOOL
        self.error(E_TYPE_MISMATCH, f"'not' expects Bool, found {t!r}", e.span)
        return ERROR

    def _binop(self, e: A.BinOp, scope: Scope) -> Type:
        lt = self.synth(e.left, scope)
        rt = self.synth(e.right, scope)
        op = e.op
        if lt is ERROR or rt is ERROR:
            return BOOL if (op in _EQUALITY or op in _ORDERING) else ERROR
        sym = op.symbol
        if op in _ARITH:
            if lt != rt:
                self.error(E_TYPE_MISMATCH,
                           f"operands of '{sym}' must have the same type, found {lt!r} and {rt!r}",
                           e.span)
                return ERROR
            allowed = (INT, FLOAT, STR) if op is A.BinOpKind.ADD else (INT, FLOAT)
            if lt not in allowed:
                self.error(E_TYPE_MISMATCH, f"'{sym}' is not defined for {lt!r}", e.span)
                return ERROR
            return lt
        if lt != rt:
            self.error(E_TYPE_MISMATCH,
                       f"'{sym}' operands must have the same type, found {lt!r} and {rt!r}", e.span)
            return BOOL
        if op in _EQUALITY:
            if lt not in (INT, FLOAT, BOOL, STR):
                self.error(E_NOT_COMPARABLE, f"'{sym}' is not defined for {lt!r}", e.span)
        elif lt not in (INT, FLOAT, STR):
            self.error(E_TYPE_MISMATCH, f"'{sym}' is not defined for {lt!r}", e.span)
        return BOOL

    def _logical(self, e: A.Logical, scope: Scope) -> Type:
        lt = self.synth(e.left, scope)
        rt = self.synth(e.right, scope)
        if lt is not ERROR and lt != BOOL:
            self.error(E_TYPE_MISMATCH,
                       f"'{e.op.symbol}' expects Bool operands, found {lt!r}", e.left.span)
        if rt is not ERROR and rt != BOOL:
            self.error(E_TYPE_MISMATCH,
                       f"'{e.op.symbol}' expects Bool operands, found {rt!r}", e.right.span)
        return BOOL

    def _if_expr(self, e: A.IfExpr, scope: Scope, expected: Type | None = None) -> Type:
        """An if-expression: branch bodies are blocks-as-values (§2.6) — statements
        followed by a tail expression, checked with the match-arm machinery so
        expected-type flow and divergent (return/break/continue) branches behave
        exactly like match arms."""
        for cond, _ in e.branches:
            self._condition(cond, scope)
        types: list[Type] = []
        for _, body in e.branches:
            t = self._arm_value(body, scope.child(), False, expected)
            if t is not None:
                types.append(t)
        t = self._arm_value(e.orelse, scope.child(), False, expected)
        if t is not None:
            types.append(t)
        result: Type | None = None
        for t in types:
            if t is ERROR:
                continue
            if result is None:
                result = t
            elif t != result:
                self.error(E_TYPE_MISMATCH,
                           f"if-expression branches must have the same type, "
                           f"found {result!r} and {t!r}", e.span)
                e.ty = ERROR
                return ERROR
        if result is not None:
            e.ty = result
        elif types:                    # every branch errored
            e.ty = ERROR
        else:                          # every branch diverges (return/break/continue)
            e.ty = expected if expected is not None else UNIT
        return e.ty

    def _lambda(self, e: A.Lambda, scope: Scope) -> Type:
        fn_scope = scope.child_frame("local")
        param_tys = tuple(self._resolve_type(p.annotation) for p in e.params)
        for p, pty in zip(e.params, param_tys):
            self._declare_checked(fn_scope, p.name, pty, False, p.span)
        saved = self.loop_depth
        self.loop_depth = 0
        if e.ret is not None:
            ret = self._resolve_type(e.ret)
            self.ret_stack.append(ret)
            self._check_fn_body(e.body, fn_scope, ret, e.span)
            self.ret_stack.pop()
        elif len(e.body) == 1 and isinstance(e.body[0], A.ExprStmt):
            # inline body: infer from the expression. `_INFER` marks the return frame so
            # `?`/`return` inside it are rejected (the inferred type is not known yet — letting
            # them through would leak an error value as the success payload).
            self.ret_stack.append(_INFER)
            ret = self.synth(e.body[0].expr, fn_scope)
            self.ret_stack.pop()
        else:
            self.error(E_UNSUPPORTED,
                       "a lambda with a block body needs a return-type annotation ('-> T')", e.span)
            for s in e.body:
                self.stmt(s, fn_scope)
            ret = ERROR
        self.loop_depth = saved
        e.n_locals = fn_scope.root.counter
        e.captures = fn_scope.root.captures
        return FnT(param_tys, ret)

    def _call(self, e: A.Call, scope: Scope) -> Type:
        if self._is_variant_call(e, scope):
            return self._construct_variant(e, scope, expected=None)
        gen = self._generic_callee(e, scope)                    # a generic fn call (M9b)
        if gen is not None:
            return self._call_generic(e, gen, scope, None, self._callee_label(e))
        if isinstance(e.func, A.Name) and scope.lookup(e.func.ident) is None:
            if e.func.ident in self.structs:
                return self._construct_struct(e, scope)
            if e.func.ident in BUILTIN_SIGS:
                return self._builtin_call(e, scope)
            if e.func.ident in _INTRINSICS:
                return self._chan_intrinsic(e, scope)
        if isinstance(e.func, A.Name) and e.func.ident == "print" and scope.lookup("print") is None:
            e.func.ty = UNIT
            if len(e.args) != 1:
                self.error(E_UNSUPPORTED,
                           f"print takes exactly one argument (got {len(e.args)})", e.span)
            for a in e.args:
                self.synth(a, scope)
            return UNIT
        fty = self.synth(e.func, scope)
        if isinstance(fty, FnT) and len(e.args) == len(fty.params):
            for a, pty in zip(e.args, fty.params):   # flow the param type into each argument
                self.check_expr(a, pty, scope)
            return fty.ret
        for a in e.args:
            self.synth(a, scope)
        if fty is ERROR:
            return ERROR
        if not isinstance(fty, FnT):
            self.error(E_NOT_CALLABLE, f"{fty!r} is not callable", e.func.span)
            return ERROR
        self.error(E_ARITY,
                   f"expected {len(fty.params)} argument(s), got {len(e.args)}", e.span)
        return fty.ret

    # -- types ---------------------------------------------------------------

    def _resolve_type(self, texpr: A.TypeExpr) -> Type:
        if isinstance(texpr, A.FnTypeExpr):
            params = tuple(self._resolve_type(p) for p in texpr.params)
            return FnT(params, self._resolve_type(texpr.ret))
        if isinstance(texpr, A.TupleTypeExpr):
            return TupleT(tuple(self._resolve_type(t) for t in texpr.items))
        if isinstance(texpr, A.NamedType):
            name, args = texpr.name, texpr.args
            if not args:
                if name in self.type_params:                # a generic parameter T (M9b)
                    return self.type_params[name]
                prim = _PRIMS.get(name)
                if prim is not None:
                    return prim
                if name in self.enum_names:                 # user enum (bare)
                    if self.enum_params.get(name):          # generic enum used without type args
                        self.error(E_UNKNOWN_TYPE,
                                   f"'{name}' expects {len(self.enum_params[name])} type argument(s)",
                                   texpr.span)
                        return ERROR
                    return EnumT(name, ())
                if name in self.struct_names:               # user struct (bare)
                    if self.struct_params.get(name):
                        self.error(E_UNKNOWN_TYPE,
                                   f"'{name}' expects {len(self.struct_params[name])} type argument(s)",
                                   texpr.span)
                        return ERROR
                    return StructT(name)
                if name in self.enums:                       # a generic prelude enum, bare
                    self.error(E_UNKNOWN_TYPE, f"enum '{name}' needs type arguments", texpr.span)
                    return ERROR
            else:
                if name in self.type_params:                # T[Int] — a param takes no args (M9b)
                    self.error(E_UNKNOWN_TYPE,
                               f"type parameter '{name}' takes no type arguments", texpr.span)
                    return ERROR
                if name == "List" and len(args) == 1:
                    return ListT(self._resolve_type(args[0]))
                if name == "Chan" and len(args) == 1:
                    return ChanT(self._resolve_type(args[0]))   # value ops arrive in M7
                if name in self.enums or name in self.enum_params:   # generic enum applied
                    params = self.enum_params.get(name, self.enums[name].type_params
                                                  if name in self.enums else ())
                    targs = tuple(self._resolve_type(a) for a in args)
                    if len(targs) != len(params):
                        self.error(E_UNKNOWN_TYPE,
                                   f"'{name}' expects {len(params)} type argument(s)", texpr.span)
                        return ERROR
                    return EnumT(name, targs)
                if name in self.structs or name in self.struct_params:   # generic struct applied
                    params = self.struct_params.get(name, self.structs[name].type_params
                                                    if name in self.structs else ())
                    targs = tuple(self._resolve_type(a) for a in args)
                    if len(targs) != len(params):
                        self.error(E_UNKNOWN_TYPE,
                                   f"'{name}' expects {len(params)} type argument(s)", texpr.span)
                        return ERROR
                    return StructT(name, targs)
        name = texpr.name if isinstance(texpr, A.NamedType) else "?"
        self.error(E_UNKNOWN_TYPE, f"unknown type '{name}'", texpr.span)
        return ERROR

    # -- mini-unifier (Fable ruling 3) --------------------------------------

    def _compatible(self, a: Type, b: Type) -> bool:
        return a is ERROR or b is ERROR or a == b

    def _unify(self, param: Type, actual: Type, sub: dict) -> bool:
        if actual is ERROR or param is ERROR:
            return True
        if isinstance(param, TypeVar):
            if param.name in sub:
                return self._compatible(sub[param.name], actual)
            sub[param.name] = actual
            return True
        if isinstance(param, EnumT) and isinstance(actual, EnumT) and param.name == actual.name \
                and len(param.args) == len(actual.args):
            return all(self._unify(p, a, sub) for p, a in zip(param.args, actual.args))
        if isinstance(param, StructT) and isinstance(actual, StructT) and param.name == actual.name \
                and len(param.args) == len(actual.args):
            return all(self._unify(p, a, sub) for p, a in zip(param.args, actual.args))
        if isinstance(param, ListT) and isinstance(actual, ListT):
            return self._unify(param.elem, actual.elem, sub)
        if isinstance(param, ChanT) and isinstance(actual, ChanT):
            return self._unify(param.elem, actual.elem, sub)
        if isinstance(param, TupleT) and isinstance(actual, TupleT) \
                and len(param.items) == len(actual.items):
            return all(self._unify(p, a, sub) for p, a in zip(param.items, actual.items))
        if isinstance(param, FnT) and isinstance(actual, FnT) \
                and len(param.params) == len(actual.params):
            return all(self._unify(p, a, sub) for p, a in zip(param.params, actual.params)) \
                and self._unify(param.ret, actual.ret, sub)
        return param == actual

    # -- variant construction -----------------------------------------------

    def _is_variant_name(self, e: A.Expr, scope: Scope) -> bool:
        return (isinstance(e, A.Name) and e.ident in self.variant_index
                and scope.lookup(e.ident) is None)

    def _is_variant_call(self, e: A.Expr, scope: Scope) -> bool:
        return isinstance(e, A.Call) and self._is_variant_name(e.func, scope)

    def _variant_result(self, enum_name: str, sub: dict, span: Span, what: str) -> Type:
        edef = self.enums[enum_name]
        args = []
        for tp in edef.type_params:
            if tp in sub:
                args.append(sub[tp])
            else:
                self.error(E_CANNOT_INFER,
                           f"cannot infer type argument '{tp}' of {enum_name} in {what}; "
                           f"add an annotation", span)
                args.append(ERROR)
        return EnumT(enum_name, tuple(args))

    def _nullary_variant(self, e: A.Name, scope: Scope, expected: Type | None) -> Type:
        enum_name, tag = self.variant_index[e.ident]
        edef = self.enums[enum_name]
        e.res = VariantRes(enum_name, e.ident, tag, 0)
        sub: dict = {}
        if isinstance(expected, EnumT) and expected.name == enum_name \
                and len(expected.args) == len(edef.type_params):
            sub = dict(zip(edef.type_params, expected.args))
        ty = self._variant_result(enum_name, sub, e.span, f"'{e.ident}'")
        e.ty = ty
        return ty

    def _construct_variant(self, e: A.Call, scope: Scope, expected: Type | None) -> Type:
        name = e.func.ident
        enum_name, tag = self.variant_index[name]
        edef = self.enums[enum_name]
        vdef = edef.variants[tag]
        e.func.res = VariantRes(enum_name, name, tag, len(vdef.payload))
        e.func.ty = UNIT
        if len(e.args) != len(vdef.payload):
            self.error(E_VARIANT_ARITY,
                       f"variant '{name}' expects {len(vdef.payload)} argument(s), "
                       f"got {len(e.args)}", e.span)
            for a in e.args:
                self.synth(a, scope)
            return ERROR
        # top-down: pin type params from the expected result type first, so payload types can
        # flow into arguments (this is what lets `let t: Tree[Int] = Node(Leaf, 1, Leaf)` check)
        sub: dict = {}
        if isinstance(expected, EnumT) and expected.name == enum_name \
                and len(expected.args) == len(edef.type_params):
            sub = dict(zip(edef.type_params, expected.args))
        for pt, a in zip(vdef.payload, e.args):
            self._check_ctor_arg(pt, a, sub, scope)
        return self._variant_result(enum_name, sub, e.span, f"'{name}(...)'")

    def _check_ctor_arg(self, pt: Type, a: A.Expr, sub: dict, scope: Scope) -> None:
        """Check one constructor argument against payload/field type `pt` under substitution `sub`.

        If `pt` is fully determined (no unbound type vars after `sub`), check the argument in
        checking mode so the expected type flows into nested generic constructors; otherwise
        synthesize and unify to extend `sub` from the argument (bidirectional-lite, M9b)."""
        spt = subst(pt, sub)
        if not self._has_typevar(spt):
            self.check_expr(a, spt, scope)
        else:
            at = self.synth(a, scope)
            if not self._unify(pt, at, sub):
                self.error(E_TYPE_MISMATCH, f"expected {subst(pt, sub)!r}, found {at!r}", a.span)

    # -- match ---------------------------------------------------------------

    def _check_match(self, m: A.Match, scope: Scope, expected: Type | None,
                     stmt_position: bool) -> Type:
        m.stmt_position = stmt_position
        S = self.synth(m.scrutinee, scope)
        if isinstance(S, EnumT) and S.name not in self.enums:
            # an enum type imported from another module: its definition (variants/tags) is not
            # in this checker's registry — cross-module enum match is deferred post-v1 (§7 M8)
            self.error(E_UNSUPPORTED,
                       f"cannot match on foreign enum type '{S.name}' "
                       f"(cross-module enums are not supported in v1)", m.scrutinee.span)
            S = ERROR
        m.scrut_slot = self._temp(scope)
        rows: list = []
        arm_types: list[Type] = []
        had_pattern_error = False
        for arm in m.arms:
            arm_scope = scope.child()
            n = len(self.errors)
            sp = self._check_pattern(arm.pattern, S, m.scrut_slot, arm_scope)
            if len(self.errors) > n:                # a malformed pattern: don't cascade
                had_pattern_error = True
            else:
                if not exhaustive.is_useful(rows, [sp], exhaustive.sort_of(S), self.enums):
                    self.error(E_UNREACHABLE, "unreachable match arm", arm.pattern.span)
                rows.append([sp])
            t = self._arm_value(arm.body, arm_scope, stmt_position, expected)
            if t is not None:
                arm_types.append(t)
        if S is not ERROR and not had_pattern_error:   # every match must be total (§2.7)
            witness = exhaustive.missing_witness(rows, exhaustive.sort_of(S), self.enums)
            if witness is not None:
                label = S.name if isinstance(S, EnumT) else repr(S)
                self.error(E_NON_EXHAUSTIVE,
                           f"match on '{label}' is not exhaustive; missing: {witness}", m.span)
        if stmt_position:
            m.ty = UNIT
            return UNIT
        result: Type | None = None
        for t in arm_types:
            if t is ERROR:
                continue
            if result is None:
                result = t
            elif t != result:
                self.error(E_TYPE_MISMATCH,
                           f"match arms must have the same type, found {result!r} and {t!r}", m.span)
                m.ty = ERROR
                return ERROR
        m.ty = result if result is not None else UNIT
        return m.ty

    def _arm_value(self, body: list[A.Stmt], scope: Scope, stmt_position: bool,
                   expected: Type | None) -> Type | None:
        for s in body[:-1]:
            self.stmt(s, scope)
        last = body[-1]
        if isinstance(last, (A.Break, A.Continue, A.Return)):
            self.stmt(last, scope)
            return None                              # divergent — contributes no type
        if isinstance(last, A.ExprStmt):
            if stmt_position:
                self.synth(last.expr, scope)
                return UNIT
            if expected is not None:                 # flow expected into the arm value (inference)
                self.check_expr(last.expr, expected, scope)
                return expected
            return self.synth(last.expr, scope)
        self.stmt(last, scope)
        return UNIT

    def _chan_payload(self, ch_ty: Type, span: Span, op: str) -> Type:
        """The element type of a channel head in a select arm, or ERROR (already reported)."""
        if ch_ty is ERROR:
            return ERROR
        if isinstance(ch_ty, ChanT):
            return ch_ty.elem
        self.error(E_TYPE_MISMATCH, f"'{op}' in select expects a channel, found {ch_ty!r}", span)
        return ERROR

    def _check_select(self, sel: A.Select, scope: Scope, expected: Type | None,
                      stmt_position: bool) -> Type:
        sel.stmt_position = stmt_position
        arm_types: list[Type] = []
        last = len(sel.arms) - 1
        for i, arm in enumerate(sel.arms):
            if arm.kind == "else" and i != last:            # else must be the single, last arm
                self.error(E_SELECT_ELSE, "select: 'else' must be the single, last arm", arm.span)
            arm_scope = scope.child()
            if arm.kind == "recv":
                payload = self._chan_payload(self.synth(arm.chan, scope), arm.chan.span, "recv")
                opt_ty = EnumT("Option", (payload,)) if payload is not ERROR else ERROR
                if arm.binder is not None:
                    arm.binder_res = self._declare_checked(
                        arm_scope, arm.binder, opt_ty, False, arm.span).resolution()
            elif arm.kind == "send":
                payload = self._chan_payload(self.synth(arm.chan, scope), arm.chan.span, "send")
                if payload is not ERROR:
                    self.check_expr(arm.value, payload, scope)
                else:
                    self.synth(arm.value, scope)            # still surface errors in the value
            t = self._arm_value(arm.body, arm_scope, stmt_position, expected)
            if t is not None:
                arm_types.append(t)
        if stmt_position:
            sel.ty = UNIT
            return UNIT
        result: Type | None = None
        for t in arm_types:
            if t is ERROR:
                continue
            if result is None:
                result = t
            elif t != result:
                self.error(E_TYPE_MISMATCH,
                           f"select arms must have the same type, found {result!r} and {t!r}",
                           sel.span)
                sel.ty = ERROR
                return ERROR
        sel.ty = result if result is not None else UNIT
        return sel.ty

    def _bind_error_binders(self, pat: A.Pattern, scope: Scope) -> None:
        """After a malformed constructor/tuple pattern, still bind every identifier binder
        (with type ERROR) so the arm body sees the names and doesn't cascade into E0111.
        Respects the prelude-name guard; nested variant/tuple sub-patterns are walked too."""
        if isinstance(pat, A.PIdent):
            if pat.name in self.variant_index:              # a nullary-variant reference, not a binder
                return
            if pat.name in _RESERVED or pat.name in _PRELUDE_NAMES:
                return
            fslot = self._temp(scope)
            slot = fslot.index if isinstance(fslot, Global) else fslot.slot
            pat.resolved = scope.declare_at(pat.name, ERROR, slot, pat.span).resolution()
        elif isinstance(pat, A.PVariant):
            for arg in pat.args:
                self._bind_error_binders(arg, scope)
        elif isinstance(pat, A.PTuple):
            for sub in pat.elems:
                self._bind_error_binders(sub, scope)

    def _check_pattern(self, pat: A.Pattern, ty: Type, value_res, scope: Scope):
        if isinstance(pat, A.PWildcard):
            return exhaustive.WILD
        if isinstance(pat, A.PLiteral):
            lit_ty = {"int": INT, "str": STR, "bool": BOOL}[pat.kind]
            if ty is not ERROR and ty != lit_ty:
                self.error(E_TYPE_MISMATCH,
                           f"pattern of type {lit_ty!r} cannot match scrutinee of type {ty!r}",
                           pat.span)
            return exhaustive.Ctor(pat.value, [])
        if isinstance(pat, A.PIdent):
            if pat.name in self.variant_index:
                enum_name, tag = self.variant_index[pat.name]
                if not (isinstance(ty, EnumT) and ty.name == enum_name):
                    self.error(E_NOT_A_VARIANT,
                               f"variant '{pat.name}' belongs to enum {enum_name}, not {ty!r}",
                               pat.span)
                    return exhaustive.WILD
                vdef = self.enums[enum_name].variants[tag]
                if len(vdef.payload) != 0:
                    self.error(E_VARIANT_ARITY,
                               f"variant '{pat.name}' takes {len(vdef.payload)} argument(s)",
                               pat.span)
                    return exhaustive.WILD
                pat.resolved = VariantRes(enum_name, pat.name, tag, 0)
                return exhaustive.Ctor(tag, [])
            if pat.name in _RESERVED or pat.name in _PRELUDE_NAMES:
                self.error(E_INTRINSIC_NOT_VALUE,
                           f"cannot rebind prelude name '{pat.name}'", pat.span)
            elif scope.local(pat.name) is not None:
                # intra-pattern duplicate: each arm checks in a fresh child scope, so a prior
                # entry here can only be another binder of this same pattern (§2.2). Sibling
                # arms have their own scopes and may legitimately reuse binder names.
                self.error(E_DUPLICATE, f"duplicate binder '{pat.name}' in pattern", pat.span)
            slot = value_res.index if isinstance(value_res, Global) else value_res.slot
            pat.resolved = scope.declare_at(pat.name, ty, slot, pat.span).resolution()
            return exhaustive.WILD
        if isinstance(pat, A.PVariant):
            if not isinstance(ty, EnumT) or pat.name not in self.variant_index \
                    or self.variant_index[pat.name][0] != ty.name:
                self.error(E_NOT_A_VARIANT, f"'{pat.name}' is not a variant of {ty!r}", pat.span)
                self._bind_error_binders(pat, scope)        # bind sub-binders so the body is quiet
                return exhaustive.WILD
            enum_name, tag = self.variant_index[pat.name]
            edef = self.enums[enum_name]
            vdef = edef.variants[tag]
            pat.tag = tag
            if len(pat.args) != len(vdef.payload):
                self.error(E_VARIANT_ARITY,
                           f"variant '{pat.name}' expects {len(vdef.payload)} argument(s), "
                           f"got {len(pat.args)}", pat.span)
                self._bind_error_binders(pat, scope)        # bind sub-binders so the body is quiet
                return exhaustive.WILD
            mapping = dict(zip(edef.type_params, ty.args))
            subs = []
            field_slots = []
            for arg, pty in zip(pat.args, vdef.payload):
                fslot = self._temp(scope)
                field_slots.append(fslot)
                subs.append(self._check_pattern(arg, subst(pty, mapping), fslot, scope))
            pat.field_slots = field_slots
            return exhaustive.Ctor(tag, subs)
        if isinstance(pat, A.PTuple):
            if not isinstance(ty, TupleT) or len(ty.items) != len(pat.elems):
                self.error(E_TYPE_MISMATCH, f"tuple pattern does not match {ty!r}", pat.span)
                self._bind_error_binders(pat, scope)        # bind sub-binders so the body is quiet
                return exhaustive.WILD
            subs = []
            field_slots = []
            for sub, ety in zip(pat.elems, ty.items):
                fslot = self._temp(scope)
                field_slots.append(fslot)
                subs.append(self._check_pattern(sub, ety, fslot, scope))
            pat.field_slots = field_slots
            return exhaustive.Ctor(exhaustive.TUPLE_KEY, subs)
        return exhaustive.WILD  # pragma: no cover

    # -- M5 expressions & statements ----------------------------------------

    def _list_lit(self, e: A.ListLit, scope: Scope) -> Type:
        if not e.elems:
            self.error(E_EMPTY_LIST, "cannot infer element type of []; add an annotation", e.span)
            return ListT(ERROR)
        elem = self.synth(e.elems[0], scope)
        for x in e.elems[1:]:                    # flow the head's type into the tail elements
            self.check_expr(x, elem, scope)
        return ListT(elem)

    def _index(self, e: A.Index, scope: Scope) -> Type:
        obj_ty = self.synth(e.obj, scope)
        idx_ty = self.synth(e.index, scope)
        if idx_ty is not ERROR and idx_ty != INT:
            self.error(E_TYPE_MISMATCH, f"list index must be Int, found {idx_ty!r}", e.index.span)
        if isinstance(obj_ty, ListT):
            return obj_ty.elem
        if obj_ty is ERROR:
            return ERROR
        self.error(E_TYPE_MISMATCH, f"cannot index a value of type {obj_ty!r}", e.span)
        return ERROR

    def _field(self, e: A.Field, scope: Scope) -> Type:
        # module-qualified access: `m.foo` where `m` is an imported module (M8)
        if (isinstance(e.obj, A.Name) and e.obj.ident in self.module_names
                and scope.lookup(e.obj.ident) is None):
            mod = self.imports.get(e.obj.ident, {})
            if e.name in mod:
                slot, ty, mutable = mod[e.name]
                if mutable:                                          # §4.1: no shared mutable state
                    self.error(E_VAR_CAPTURE,
                               f"cannot access mutable binding '{e.obj.ident}.{e.name}' "
                               f"from another module", e.span)
                    e.res = ERROR_RES
                    return ERROR
                if isinstance(ty, FnT) and self._has_typevar(ty):    # generic fn: call-only (M9b)
                    self.error(E_GENERIC_NOT_VALUE,
                               f"generic function '{e.obj.ident}.{e.name}' is not a first-class value",
                               e.span, hint="call it directly — type arguments are always inferred")
                    e.res = ERROR_RES
                    return ERROR
                e.res = ImportedGlobal(e.obj.ident, slot)
                return ty
            self.error(E_NOT_A_MODULE_MEMBER,
                       f"module '{e.obj.ident}' has no member '{e.name}'", e.span)
            return ERROR
        obj_ty = self.synth(e.obj, scope)
        if isinstance(obj_ty, StructT):
            sdef = self.structs.get(obj_ty.name)
            if sdef is not None:
                idx = sdef.field_index(e.name)
                if idx >= 0:
                    e.res = StructField(idx)
                    fty = sdef.field_type(e.name)
                    if sdef.type_params:                         # substitute the instance's args
                        return subst(fty, dict(zip(sdef.type_params, obj_ty.args)))
                    return fty
            self.error(E_UNKNOWN_NAME, f"struct '{obj_ty.name}' has no field '{e.name}'", e.span)
            return ERROR
        if obj_ty is ERROR:
            return ERROR
        self.error(E_TYPE_MISMATCH, f"cannot access field '{e.name}' on {obj_ty!r}", e.span)
        return ERROR

    def _for(self, s: A.For, scope: Scope) -> None:
        it_ty = self.synth(s.iterable, scope)
        elem: Type = ERROR
        if isinstance(it_ty, ListT):
            elem = it_ty.elem
        elif it_ty is not ERROR:
            self.error(E_TYPE_MISMATCH, f"'for' expects a List, found {it_ty!r}", s.iterable.span)
        s.hidden = (self._temp(scope), self._temp(scope), self._temp(scope))   # xs, i, n temps
        child = scope.child()
        s.var_slot = self._declare_checked(child, s.var, elem, False, s.span).resolution()
        self.loop_depth += 1
        for st in s.body:
            self.stmt(st, child)
        self.loop_depth -= 1

    def _check_let_tuple(self, s: A.LetTuple, scope: Scope) -> None:
        if s.annotation is not None:
            declared = self._resolve_type(s.annotation)
            self.check_expr(s.value, declared, scope)
            vty = declared
        else:
            vty = self.synth(s.value, scope)
        s.tmp = self._temp(scope)
        if isinstance(vty, TupleT) and len(vty.items) == len(s.names):
            elems = list(vty.items)
        else:
            if vty is not ERROR:
                self.error(E_TYPE_MISMATCH,
                           f"cannot destructure {vty!r} into {len(s.names)} names", s.span)
            elems = [ERROR] * len(s.names)
        seen: set[str] = set()
        res_list = []
        for n, t in zip(s.names, elems):
            if n in seen:
                self.error(E_DUPLICATE, f"duplicate name '{n}' in tuple binding", s.span)
                res_list.append(scope.declare(n, t, False, s.span).resolution())
            else:
                seen.add(n)
                res_list.append(self._declare_checked(scope, n, t, False, s.span).resolution())
        s.res_list = res_list

    def _construct_struct(self, e: A.Call, scope: Scope, expected: Type | None = None) -> Type:
        sdef = self.structs[e.func.ident]
        e.func.res = StructRes(sdef.name, len(sdef.fields))
        e.func.ty = UNIT
        if len(e.args) != len(sdef.fields):
            self.error(E_VARIANT_ARITY,
                       f"struct '{sdef.name}' expects {len(sdef.fields)} field(s), "
                       f"got {len(e.args)}", e.span)
            for a in e.args:
                self.synth(a, scope)
            return StructT(sdef.name, (ERROR,) * len(sdef.type_params))
        if not sdef.type_params:                                 # monomorphic struct: fast path
            for (fname, fty), a in zip(sdef.fields, e.args):
                self.require(self.synth(a, scope), fty, a.span)
            return StructT(sdef.name)
        sub: dict = {}                                           # generic struct: infer type args
        if isinstance(expected, StructT) and expected.name == sdef.name \
                and len(expected.args) == len(sdef.type_params):
            sub = dict(zip(sdef.type_params, expected.args))     # top-down first (flows into fields)
        for (fname, fty), a in zip(sdef.fields, e.args):
            self._check_ctor_arg(fty, a, sub, scope)
        args = []
        for tp in sdef.type_params:
            if tp in sub:
                args.append(sub[tp])
            else:
                self.error(E_CANNOT_INFER, f"cannot infer type argument '{tp}' of {sdef.name} in "
                           f"'{sdef.name}(...)'; add an annotation", e.span)
                args.append(ERROR)
        return StructT(sdef.name, tuple(args))

    def _try(self, e: A.Try, scope: Scope) -> Type:
        t = self.synth(e.operand, scope)
        if t is ERROR:
            return ERROR
        if not self.ret_stack:
            self.error(E_QUESTION, "'?' is only valid inside a function", e.span)
            return ERROR
        if self.ret_stack[-1] is _INFER:
            self.error(E_QUESTION,
                       "'?' inside a lambda without a return-type annotation; add '-> T'", e.span)
            return ERROR
        R = self.ret_stack[-1]
        if isinstance(t, EnumT) and t.name == "Result":
            if isinstance(R, EnumT) and R.name == "Result" and R.args[1] == t.args[1]:
                return t.args[0]
            self.error(E_QUESTION,
                       f"'?' on Result[_, {t.args[1]!r}] requires the function to return "
                       f"Result[_, {t.args[1]!r}], but it returns {R!r}", e.span)
            return ERROR
        if isinstance(t, EnumT) and t.name == "Option":
            if isinstance(R, EnumT) and R.name == "Option":
                return t.args[0]
            self.error(E_QUESTION,
                       f"'?' on Option requires the function to return Option[_], "
                       f"but it returns {R!r}", e.span)
            return ERROR
        self.error(E_QUESTION, f"'?' expects a Result or Option, found {t!r}", e.span)
        return ERROR

    def _builtin_call(self, e: A.Call, scope: Scope) -> Type:
        name = e.func.ident
        e.func.res = Builtin(name)
        e.func.ty = UNIT
        arg_tys = [self.synth(a, scope) for a in e.args]
        for sig in BUILTIN_SIGS[name]:
            if len(sig.params) != len(e.args):
                continue
            sub: dict = {}
            if all(self._unify(p, a, sub) for p, a in zip(sig.params, arg_tys)):
                return subst(sig.ret, sub)
        self.error(E_TYPE_MISMATCH,
                   f"no matching overload for '{name}' with argument types "
                   f"{tuple(repr(t) for t in arg_tys)}", e.span)
        return ERROR

    def _chan_intrinsic(self, e: A.Call, scope: Scope) -> Type:
        name = e.func.ident
        e.func.ty = UNIT               # the compiler special-cases the name → opcode (not CALL)
        arg_tys = [self.synth(a, scope) for a in e.args]
        for sig in _INTRINSICS[name]:
            if len(sig.params) != len(e.args):
                continue
            sub: dict = {}
            if all(self._unify(p, a, sub) for p, a in zip(sig.params, arg_tys)):
                return subst(sig.ret, sub)
        self.error(E_TYPE_MISMATCH,
                   f"invalid arguments to '{name}' {tuple(repr(t) for t in arg_tys)}", e.span)
        return ERROR

    def _spawn(self, s: A.Spawn, scope: Scope) -> None:
        t = s.target
        if isinstance(t, A.Lambda):
            fty = self.synth(t, scope)
            if isinstance(fty, FnT):
                if fty.params:
                    self.error(E_ARITY, "a spawned lambda must take no arguments", s.span)
                if fty.ret is not ERROR and fty.ret != UNIT:
                    self.error(E_TYPE_MISMATCH,
                               f"a spawned function must return Unit, not {fty.ret!r}", s.span)
            return
        if not isinstance(t, A.Call):
            self.error(E_UNSUPPORTED, "spawn expects a function call or a lambda", s.span)
            return
        gen = self._generic_callee(t, scope)                    # spawn of a generic fn call (M9b)
        if gen is not None:
            ret = self._call_generic(t, gen, scope, None, self._callee_label(t))
            if ret is not ERROR and ret != UNIT:
                self.error(E_TYPE_MISMATCH,
                           f"a spawned function must return Unit, not {ret!r}", s.span)
            return
        if isinstance(t.func, A.Name) and scope.lookup(t.func.ident) is None:
            name = t.func.ident
            if (name in self.variant_index or name in self.structs
                    or name in BUILTIN_SIGS or name in _INTRINSICS or name == "print"):
                self.error(E_UNSUPPORTED,
                           f"cannot spawn '{name}'; spawn a user-defined function or lambda", s.span)
                return
        fty = self.synth(t.func, scope)
        if isinstance(fty, FnT) and len(t.args) == len(fty.params):
            if fty.ret is not ERROR and fty.ret != UNIT:
                self.error(E_TYPE_MISMATCH,
                           f"a spawned function must return Unit, not {fty.ret!r}", s.span)
            for a, pty in zip(t.args, fty.params):   # flow the param type into each argument
                self.check_expr(a, pty, scope)
            return
        for a in t.args:
            self.synth(a, scope)
        if not isinstance(fty, FnT):
            if fty is not ERROR:
                self.error(E_NOT_CALLABLE, "spawn target is not a function", t.func.span)
            return
        if fty.ret is not ERROR and fty.ret != UNIT:
            self.error(E_TYPE_MISMATCH,
                       f"a spawned function must return Unit, not {fty.ret!r}", s.span)
        self.error(E_ARITY,
                   f"expected {len(fty.params)} argument(s), got {len(t.args)}", s.span)


def check(module: A.Module, module_scope: Scope | None = None,
          imports: dict | None = None) -> A.Module:
    """Check and annotate `module` in place. Raises CheckErrors on any error.

    Two-pass (Model B): hoist all top-level `fn` signatures, then check every item in source
    order so a fn body sees all fns plus the module value-bindings declared textually above it.
    """
    scope = module_scope if module_scope is not None else Scope(FrameRoot("global"))
    c = _Checker()
    c.imports = imports if imports is not None else {}
    # register imported module names (import is a top-level decl only)
    for d in module.decls:
        if isinstance(d, A.Import):
            if imports is None:
                c.error(E_IMPORT_UNSUPPORTED,
                        "import is only available when running a source file", d.span)
            elif d.name not in c.imports:
                c.error(E_MODULE_NOT_FOUND, f"cannot find module '{d.name}'", d.span)
            else:
                c.module_names.add(d.name)
    # pass 0: collect all type names + their declared arities (forward refs), then register defs
    for d in module.decls:
        if isinstance(d, A.EnumDecl):
            c.enum_names.add(d.name)
            c.enum_params[d.name] = tuple(d.type_params)
        elif isinstance(d, A.StructDecl):
            c.struct_names.add(d.name)
            c.struct_params[d.name] = tuple(d.type_params)
        elif isinstance(d, A.FnDecl):
            c.fn_names.add(d.name)
    for d in module.decls:
        if isinstance(d, A.EnumDecl):
            c.register_enum(d)
        elif isinstance(d, A.StructDecl):
            c.register_struct(d)
    # pass 1: hoist top-level fn signatures (may reference enum types)
    for d in module.decls:
        if isinstance(d, A.FnDecl):
            c.hoist_fn(d, scope)
    # pass 2: check items in source order
    for d in module.decls:
        c.item(d, scope)
    if c.errors:
        raise CheckErrors(c.errors)
    module.n_globals = scope.root.counter
    module.main_slot = c.main_slot
    # export this module's top-level value bindings for importers: name -> (slot, type)
    module.symbols = {name: (info.slot, info.ty, info.mutable)
                      for name, info in scope.names.items()}
    return module
