"""AST -> bytecode compiler (DESIGN.md §5.3, §5.5, §6.2). No IR, no optimizer.

Assert-only on shapes — the checker proved the AST well-formed and filled every `res`, plus
function metadata (`n_locals`, `captures`, `fn_ty`). Functions compile to nested `Code`
objects stored in the enclosing code's constant pool; a closure is built by pushing its
captured values (as the enclosing frame sees them) then `MAKE_CLOSURE`. Function bodies use
block-as-value lowering (Fable ruling 4): the tail expression is `RETURN`ed; a Unit function
falls off the end into an implicit `return ()`.
"""

from __future__ import annotations

from tantu import ast_nodes as A
from tantu.bytecode import Op, Instr, Code
from tantu.errors import Span
from tantu.resolve import (
    Global, Local, Capture, VariantRes, StructRes, Builtin as BuiltinRes, ImportedGlobal,
)
from tantu.values import Builtin as RtBuiltin
from tantu.linker import CrossRef
from tantu.ttypes import UNIT

_BINOP = {
    A.BinOpKind.ADD: Op.ADD, A.BinOpKind.SUB: Op.SUB, A.BinOpKind.MUL: Op.MUL,
    A.BinOpKind.DIV: Op.DIV, A.BinOpKind.MOD: Op.MOD,
    A.BinOpKind.EQ: Op.EQ, A.BinOpKind.NE: Op.NE, A.BinOpKind.LT: Op.LT,
    A.BinOpKind.LE: Op.LE, A.BinOpKind.GT: Op.GT, A.BinOpKind.GE: Op.GE,
}


def _is_print(e: A.Expr) -> bool:
    return isinstance(e, A.Call) and isinstance(e.func, A.Name) and e.func.ident == "print"


class _LoopCtx:
    __slots__ = ("breaks", "continues")

    def __init__(self) -> None:
        self.breaks: list[int] = []       # JUMP placeholders → loop end
        self.continues: list[int] = []    # JUMP placeholders → continue target


class _Compiler:
    """Compiles one frame (the module, or a single function body) into a Code object."""

    def __init__(self) -> None:
        self.instrs: list[Instr] = []
        self.consts: list[object] = []
        self.spans: list[Span] = []
        self.loops: list[_LoopCtx] = []

    def emit(self, op: Op, arg: int | tuple[int, ...] | None, span: Span) -> int:
        idx = len(self.instrs)
        self.instrs.append(Instr(op, arg))
        self.spans.append(span)
        return idx

    def emit_jump(self, op: Op, span: Span) -> int:
        return self.emit(op, None, span)

    def patch(self, index: int, target: int) -> None:
        self.instrs[index] = Instr(self.instrs[index].op, target)

    def here(self) -> int:
        return len(self.instrs)

    def const(self, value: object) -> int:
        if not isinstance(value, Code):  # Code objects are distinct even if identical — never dedup
            for i, c in enumerate(self.consts):
                if type(c) is type(value) and c == value:
                    return i
        self.consts.append(value)
        return len(self.consts) - 1

    # -- statements ----------------------------------------------------------

    def stmt(self, s: A.Stmt, *, echo: bool = False) -> None:
        if isinstance(s, A.Let):
            self.expr(s.value)
            self._store(s.res, s.value.span)
        elif isinstance(s, A.Assign):
            self.expr(s.value)
            self._store(s.res, s.value.span)
        elif isinstance(s, A.ExprStmt):
            e = s.expr
            self.expr(e)
            if echo and not _is_print(e):
                self.emit(Op.PRINT, None, e.span)
            self.emit(Op.POP, None, e.span)
        elif isinstance(s, A.If):
            self._if_stmt(s)
        elif isinstance(s, A.While):
            self._while(s)
        elif isinstance(s, A.Loop):
            self._loop(s)
        elif isinstance(s, A.Break):
            self.loops[-1].breaks.append(self.emit_jump(Op.JUMP, s.span))
        elif isinstance(s, A.Continue):
            self.loops[-1].continues.append(self.emit_jump(Op.JUMP, s.span))
        elif isinstance(s, A.For):
            self._for(s)
        elif isinstance(s, A.LetTuple):
            self._let_tuple(s)
        elif isinstance(s, A.Spawn):
            self._spawn(s)
        elif isinstance(s, A.Return):
            self._return(s)
        elif isinstance(s, A.FnDecl):
            self._closure(s.name, s.params, s.body, s.n_locals, s.captures,
                          s.fn_ty.ret == UNIT, s.span)
            self._store(s.res, s.span)
        elif isinstance(s, (A.EnumDecl, A.StructDecl, A.Import)):
            pass   # types / imports: no runtime code (imports run via the linker's entry)
        else:  # pragma: no cover
            raise AssertionError(f"compiler: unexpected statement {type(s).__name__}")

    def _store(self, res: object, span: Span) -> None:
        if isinstance(res, Global):
            self.emit(Op.STORE_GLOBAL, res.index, span)
        elif isinstance(res, Local):
            self.emit(Op.STORE_LOCAL, res.slot, span)
        else:  # pragma: no cover
            raise AssertionError(f"compiler: cannot store to {res!r}")

    def _return(self, s: A.Return) -> None:
        if s.value is not None:
            self.expr(s.value)
        else:
            self.emit(Op.LOAD_CONST, self.const(None), s.span)
        self.emit(Op.RETURN, None, s.span)

    def _block(self, stmts: list[A.Stmt]) -> None:
        for st in stmts:
            self.stmt(st)

    def _if_stmt(self, s: A.If) -> None:
        end_jumps: list[int] = []
        n = len(s.branches)
        for i, (cond, body) in enumerate(s.branches):
            self.expr(cond)
            skip = self.emit_jump(Op.JUMP_IF_FALSE, cond.span)
            self._block(body)
            if i + 1 < n or s.orelse is not None:
                end_jumps.append(self.emit_jump(Op.JUMP, s.span))
            self.patch(skip, self.here())
        if s.orelse is not None:
            self._block(s.orelse)
        end = self.here()
        for j in end_jumps:
            self.patch(j, end)

    def _while(self, s: A.While) -> None:
        ctx = _LoopCtx()
        self.loops.append(ctx)
        top = self.here()
        self.expr(s.cond)
        exit_jump = self.emit_jump(Op.JUMP_IF_FALSE, s.cond.span)
        self._block(s.body)
        self.emit(Op.JUMP, top, s.span)
        end = self.here()
        self.patch(exit_jump, end)
        self.loops.pop()
        for b in ctx.breaks:
            self.patch(b, end)
        for c in ctx.continues:
            self.patch(c, top)

    def _loop(self, s: A.Loop) -> None:
        ctx = _LoopCtx()
        self.loops.append(ctx)
        top = self.here()
        self._block(s.body)
        self.emit(Op.JUMP, top, s.span)
        end = self.here()
        self.loops.pop()
        for b in ctx.breaks:
            self.patch(b, end)
        for c in ctx.continues:
            self.patch(c, top)

    def _for(self, s: A.For) -> None:
        # desugar (DESIGN.md §5.3): h_xs = <iter>; h_i = 0; h_n = len(h_xs);
        # loop: if h_i < h_n: item = h_xs[h_i]; <body>; h_i = h_i + 1; goto loop
        h_xs, h_i, h_n = s.hidden
        sp = s.span
        self.expr(s.iterable)
        self._store(h_xs, sp)
        self.emit(Op.LOAD_CONST, self.const(0), sp)
        self._store(h_i, sp)
        self.emit(Op.LOAD_CONST, self.const(RtBuiltin("len", 1)), sp)   # h_n = len(h_xs)
        self._load_res(h_xs, sp)
        self.emit(Op.CALL, 1, sp)
        self._store(h_n, sp)
        ctx = _LoopCtx()
        self.loops.append(ctx)
        top = self.here()
        self._load_res(h_i, sp)
        self._load_res(h_n, sp)
        self.emit(Op.LT, None, sp)
        exit_jump = self.emit_jump(Op.JUMP_IF_FALSE, sp)
        self._load_res(h_xs, sp)
        self._load_res(h_i, sp)
        self.emit(Op.INDEX, None, sp)
        self._store(s.var_slot, sp)
        self._block(s.body)
        incr = self.here()
        self._load_res(h_i, sp)
        self.emit(Op.LOAD_CONST, self.const(1), sp)
        self.emit(Op.ADD, None, sp)
        self._store(h_i, sp)
        self.emit(Op.JUMP, top, sp)
        end = self.here()
        self.patch(exit_jump, end)
        self.loops.pop()
        for b in ctx.breaks:
            self.patch(b, end)
        for c in ctx.continues:
            self.patch(c, incr)      # `continue` jumps to the increment

    def _let_tuple(self, s: A.LetTuple) -> None:
        self.expr(s.value)
        self._store(s.tmp, s.span)
        for i, res in enumerate(s.res_list):
            self._load_res(s.tmp, s.span)
            self.emit(Op.GET_FIELD, i, s.span)
            self._store(res, s.span)

    def _spawn(self, s: A.Spawn) -> None:
        t = s.target
        if isinstance(t, A.Lambda):
            self.expr(t)                           # MAKE_CLOSURE → the closure on the stack
            self.emit(Op.SPAWN, 0, s.span)
        else:                                       # a Call: push the fn value + args, then SPAWN
            self.expr(t.func)
            for a in t.args:
                self.expr(a)
            self.emit(Op.SPAWN, len(t.args), s.span)

    # -- function bodies (block-as-value) ------------------------------------

    def _closure(self, name: str, params: list, body: list, n_locals: int,
                 captures: list, ret_unit: bool, span: Span) -> None:
        code = self._compile_function(name, params, body, n_locals, ret_unit)
        code_idx = self.const(code)
        for src in captures:                       # push captures as THIS frame sees them
            if isinstance(src, Local):
                self.emit(Op.LOAD_LOCAL, src.slot, span)
            elif isinstance(src, Capture):
                self.emit(Op.LOAD_CAPTURE, src.index, span)
            else:  # pragma: no cover
                raise AssertionError(f"compiler: bad capture source {src!r}")
        self.emit(Op.MAKE_CLOSURE, (code_idx, len(captures)), span)

    def _compile_function(self, name: str, params: list, body: list,
                          n_locals: int, ret_unit: bool) -> Code:
        sub = _Compiler()
        end = body[-1].span
        if ret_unit:
            for s in body:
                sub.stmt(s)
            sub.emit(Op.LOAD_CONST, sub.const(None), end)   # implicit return ()
            sub.emit(Op.RETURN, None, end)
        else:
            sub._tail_block(body)
        return Code(name, len(params), n_locals, 0, sub.instrs, sub.consts, sub.spans)

    def _tail_block(self, body: list[A.Stmt]) -> None:
        for s in body[:-1]:
            self.stmt(s)
        self._tail(body[-1])

    def _tail(self, s: A.Stmt) -> None:
        if isinstance(s, A.Return):
            self._return(s)
        elif isinstance(s, A.ExprStmt):
            self.expr(s.expr)
            self.emit(Op.RETURN, None, s.span)
        elif isinstance(s, A.If):                    # value-if: each branch RETURNs
            for cond, body in s.branches:
                self.expr(cond)
                skip = self.emit_jump(Op.JUMP_IF_FALSE, cond.span)
                self._tail_block(body)
                self.patch(skip, self.here())
            self._tail_block(s.orelse)
        else:  # pragma: no cover — checker's E0108 guarantees a yielding tail
            raise AssertionError(f"compiler: non-yielding tail {type(s).__name__}")

    # -- expressions ---------------------------------------------------------

    def expr(self, e: A.Expr) -> None:
        if isinstance(e, (A.IntLit, A.FloatLit, A.StrLit, A.BoolLit)):
            self.emit(Op.LOAD_CONST, self.const(e.value), e.span)
        elif isinstance(e, A.UnitLit):
            self.emit(Op.LOAD_CONST, self.const(None), e.span)     # unit is runtime None
        elif isinstance(e, A.Name):
            self._load(e)
        elif isinstance(e, A.Unary):
            self.expr(e.operand)
            self.emit(Op.NEG if e.op is A.UnOpKind.NEG else Op.NOT, None, e.span)
        elif isinstance(e, A.BinOp):
            self.expr(e.left)
            self.expr(e.right)
            self.emit(_BINOP[e.op], None, e.span)
        elif isinstance(e, A.Logical):
            self._logical(e)
        elif isinstance(e, A.IfExpr):
            self._if_expr(e)
        elif isinstance(e, A.Lambda):
            self._closure("<lambda>", e.params, e.body, e.n_locals, e.captures,
                          e.ty.ret == UNIT, e.span)
        elif isinstance(e, A.Match):
            self._match(e)
        elif isinstance(e, A.Select):
            self._select(e)
        elif isinstance(e, A.ListLit):
            for x in e.elems:
                self.expr(x)
            self.emit(Op.BUILD_LIST, len(e.elems), e.span)
        elif isinstance(e, A.TupleLit):
            for x in e.elems:
                self.expr(x)
            self.emit(Op.BUILD_TUPLE, len(e.elems), e.span)
        elif isinstance(e, A.Index):
            self.expr(e.obj)
            self.expr(e.index)
            self.emit(Op.INDEX, None, e.span)
        elif isinstance(e, A.Field):
            if isinstance(e.res, ImportedGlobal):                 # m.foo — cross-module global
                self.emit(Op.LOAD_GLOBAL, CrossRef(e.res.module, e.res.slot), e.span)
            else:                                                 # StructField — record field
                self.expr(e.obj)
                self.emit(Op.GET_FIELD, e.res.index, e.span)
        elif isinstance(e, A.ChanNew):
            if e.cap is not None:
                self.expr(e.cap)
            else:
                self.emit(Op.LOAD_CONST, self.const(0), e.span)
            self.emit(Op.CHAN_NEW, None, e.span)
        elif isinstance(e, A.Try):
            # `?` desugar: unwrap Ok/Some (tag 0); else early-return the Err/None unchanged
            self.expr(e.operand)
            self.emit(Op.DUP, None, e.span)
            self.emit(Op.TEST_VARIANT, 0, e.span)          # tag 0 = Ok / Some
            ok = self.emit_jump(Op.JUMP_IF_TRUE, e.span)
            self.emit(Op.RETURN, None, e.span)             # Err/None: return it unchanged
            self.patch(ok, self.here())
            self.emit(Op.UNPACK_VARIANT, 1, e.span)        # Ok/Some: leave the payload
        elif isinstance(e, A.Call):
            self._call(e)
        else:  # pragma: no cover
            raise AssertionError(f"compiler: unexpected expression {type(e).__name__}")

    def _load(self, e: A.Name) -> None:
        res = e.res
        if isinstance(res, VariantRes):
            self._make_variant(res, e.span)          # nullary variant value (None, Red)
        elif isinstance(res, Global):
            self.emit(Op.LOAD_GLOBAL, res.index, e.span)
        elif isinstance(res, Local):
            self.emit(Op.LOAD_LOCAL, res.slot, e.span)
        elif isinstance(res, Capture):
            self.emit(Op.LOAD_CAPTURE, res.index, e.span)
        else:  # pragma: no cover
            raise AssertionError(f"compiler: cannot load {res!r} for '{e.ident}'")

    _CHAN_OPS = {"send": Op.CHAN_SEND, "recv": Op.CHAN_RECV,
                 "close": Op.CHAN_CLOSE, "sleep": Op.SLEEP}

    def _call(self, e: A.Call) -> None:
        if _is_print(e):
            self.expr(e.args[0])
            self.emit(Op.PRINT, None, e.span)
            return
        if isinstance(e.func, A.Name) and e.func.ident in self._CHAN_OPS:
            for a in e.args:                       # chan intrinsics → dedicated opcodes (not CALL)
                self.expr(a)
            self.emit(self._CHAN_OPS[e.func.ident], None, e.span)
            return
        if isinstance(e.func, A.Name) and isinstance(e.func.res, VariantRes):
            for a in e.args:
                self.expr(a)
            self._make_variant(e.func.res, e.span)
            return
        if isinstance(e.func, A.Name) and isinstance(e.func.res, StructRes):
            for a in e.args:
                self.expr(a)
            desc = (e.func.res.struct_name, e.func.res.arity)
            self.emit(Op.MAKE_RECORD, self.const(desc), e.span)
            return
        if isinstance(e.func, A.Name) and isinstance(e.func.res, BuiltinRes):
            self.emit(Op.LOAD_CONST, self.const(RtBuiltin(e.func.res.name, len(e.args))), e.span)
            for a in e.args:
                self.expr(a)
            self.emit(Op.CALL, len(e.args), e.span)
            return
        self.expr(e.func)
        for a in e.args:
            self.expr(a)
        self.emit(Op.CALL, len(e.args), e.span)

    def _make_variant(self, res: VariantRes, span: Span) -> None:
        desc = (res.enum_name, res.variant_name, res.tag, res.arity)
        self.emit(Op.MAKE_VARIANT, self.const(desc), span)

    # -- match ---------------------------------------------------------------

    def _match(self, m: A.Match) -> None:
        self.expr(m.scrutinee)
        self._store(m.scrut_slot, m.span)
        end_jumps: list[int] = []
        for arm in m.arms:
            fails: list[int] = []
            self._pattern_tests(arm.pattern, m.scrut_slot, fails)
            self._arm_body_value(arm.body)
            end_jumps.append(self.emit_jump(Op.JUMP, m.span))
            for f in fails:
                self.patch(f, self.here())
        self.emit(Op.MATCH_FAIL, None, m.span)       # safety net (checker proved exhaustive)
        end = self.here()
        for j in end_jumps:
            self.patch(j, end)

    # -- select (M9a) --------------------------------------------------------

    def _select(self, e: A.Select) -> None:
        """Push per-arm operands, poll/park via SELECT, then dispatch to the chosen arm.

        Operands go in arm order (recv: chan; send: chan, value); `else` contributes none.
        SELECT (arg = has_else, *kinds) leaves `(case_index, payload)`; the always-following
        SELECT_DISPATCH pops it, pushes `payload`, and jumps to the arm. `else`'s case index is
        the channel-arm count, so its target is the last table entry (the checker guarantees an
        `else` is last). Arms leave a value (match's arm-body rules) — the caller's context (a
        `let`, an enclosing expr, or ExprStmt's POP) consumes it.
        """
        kinds: list[int] = []
        has_else = 0
        for arm in e.arms:
            if arm.kind == "recv":
                self.expr(arm.chan)
                kinds.append(0)
            elif arm.kind == "send":
                self.expr(arm.chan)
                self.expr(arm.value)
                kinds.append(1)
            else:
                has_else = 1
        self.emit(Op.SELECT, (has_else, *kinds), e.span)
        dispatch = self.emit(Op.SELECT_DISPATCH, None, e.span)
        targets: list[int] = []
        end_jumps: list[int] = []
        n = len(e.arms)
        for i, arm in enumerate(e.arms):
            targets.append(self.here())
            if arm.kind == "recv" and arm.binder is not None:
                self._store(arm.binder_res, arm.span)          # bind the Option[T] payload
            else:
                self.emit(Op.POP, None, arm.span)              # discard the payload (Unit/unused)
            self._arm_body_value(arm.body)
            if i != n - 1:                                     # last arm falls through to end
                end_jumps.append(self.emit_jump(Op.JUMP, e.span))
        end = self.here()
        for j in end_jumps:
            self.patch(j, end)
        self.instrs[dispatch] = Instr(Op.SELECT_DISPATCH, tuple(targets))

    def _arm_body_value(self, body: list[A.Stmt]) -> None:
        for s in body[:-1]:
            self.stmt(s)
        last = body[-1]
        if isinstance(last, A.ExprStmt):
            self.expr(last.expr)                     # leave the arm's value
        elif isinstance(last, (A.Return, A.Break, A.Continue)):
            self.stmt(last)                          # divergent — transfers control, no value
        else:
            self.stmt(last)
            self.emit(Op.LOAD_CONST, self.const(None), last.span)  # Unit tail

    def _pattern_tests(self, pat: A.Pattern, value_res, fails: list[int]) -> None:
        if isinstance(pat, A.PWildcard):
            return
        if isinstance(pat, A.PIdent):
            if isinstance(pat.resolved, VariantRes):
                self._load_res(value_res, pat.span)
                self.emit(Op.TEST_VARIANT, pat.resolved.tag, pat.span)
                fails.append(self.emit_jump(Op.JUMP_IF_FALSE, pat.span))
            # else binding: the value already lives at value_res's slot (= the binding slot)
            return
        if isinstance(pat, A.PLiteral):
            self._load_res(value_res, pat.span)
            self.emit(Op.LOAD_CONST, self.const(pat.value), pat.span)
            self.emit(Op.EQ, None, pat.span)
            fails.append(self.emit_jump(Op.JUMP_IF_FALSE, pat.span))
            return
        if isinstance(pat, A.PVariant):
            self._load_res(value_res, pat.span)
            self.emit(Op.TEST_VARIANT, pat.tag, pat.span)
            fails.append(self.emit_jump(Op.JUMP_IF_FALSE, pat.span))
            if pat.args:
                self._load_res(value_res, pat.span)
                self.emit(Op.UNPACK_VARIANT, len(pat.args), pat.span)
                for fslot in reversed(pat.field_slots):    # UNPACK pushes f1..fa (fa on top)
                    self._store(fslot, pat.span)
                for arg, fslot in zip(pat.args, pat.field_slots):
                    self._pattern_tests(arg, fslot, fails)
            return
        if isinstance(pat, A.PTuple):
            # tuples always match structurally: extract each element, then recurse
            for i, fslot in enumerate(pat.field_slots):
                self._load_res(value_res, pat.span)
                self.emit(Op.GET_FIELD, i, pat.span)
                self._store(fslot, pat.span)
            for sub, fslot in zip(pat.elems, pat.field_slots):
                self._pattern_tests(sub, fslot, fails)
            return
        raise AssertionError(f"compiler: unexpected pattern {type(pat).__name__}")  # pragma: no cover

    def _load_res(self, res, span: Span) -> None:
        if isinstance(res, Global):
            self.emit(Op.LOAD_GLOBAL, res.index, span)
        else:
            self.emit(Op.LOAD_LOCAL, res.slot, span)

    def _logical(self, e: A.Logical) -> None:
        self.expr(e.left)
        self.emit(Op.DUP, None, e.span)
        short = self.emit_jump(
            Op.JUMP_IF_FALSE if e.op is A.LogicalKind.AND else Op.JUMP_IF_TRUE, e.span)
        self.emit(Op.POP, None, e.span)
        self.expr(e.right)
        self.patch(short, self.here())

    def _if_expr(self, e: A.IfExpr) -> None:
        end_jumps: list[int] = []
        for cond, body in e.branches:
            self.expr(cond)
            skip = self.emit_jump(Op.JUMP_IF_FALSE, cond.span)
            self._arm_body_value(body)                # block-as-value: stmts + tail expr
            end_jumps.append(self.emit_jump(Op.JUMP, e.span))
            self.patch(skip, self.here())
        self._arm_body_value(e.orelse)
        end = self.here()
        for j in end_jumps:
            self.patch(j, end)


def compile_module(module: A.Module, *, repl: bool = False,
                   is_root: bool = True, name: str = "<module>") -> Code:
    """Compile a checked Module into its Code object (DESIGN.md §5.5, §7 M8).

    A root module ends in the `main()` epilogue + HALT (the program's entry); an imported
    module compiles to an init function ending in `RETURN Unit`, which the linker's `<entry>`
    calls in topological order.
    """
    c = _Compiler()
    last = len(module.decls) - 1
    for i, s in enumerate(module.decls):
        c.stmt(s, echo=(repl and i == last))
    if is_root:
        if module.main_slot is not None and not repl:      # entry epilogue: call main()
            c.emit(Op.LOAD_GLOBAL, module.main_slot, module.span)
            c.emit(Op.CALL, 0, module.span)
            c.emit(Op.POP, None, module.span)
        c.emit(Op.HALT, None, module.span)
    else:
        c.emit(Op.LOAD_CONST, c.const(None), module.span)  # imported module init -> Unit
        c.emit(Op.RETURN, None, module.span)
    return Code(name, 0, 0, module.n_globals, c.instrs, c.consts, c.spans)
