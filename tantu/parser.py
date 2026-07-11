"""Recursive-descent parser (DESIGN.md §2.11, §6.2).

Expression ladder (precedence low→high): if-expr / or / and / not / comparison (non-assoc)
/ add / mul / unary / postfix (call) / primary. Statements: let/var, assignment, if/elif/else
(stmt), while, loop, break, continue, and expression statements. Blocks are either a single
inline simple statement after ':' or a NEWLINE + INDENT … DEDENT suite.

`if` is a statement in statement position and an expression in expression position (e.g. a
`let` RHS) — the two entry points disambiguate by context. Comparison is non-associative:
`a < b < c` is a parse error. Block-form if-*expression* and block-as-value arrive in M3.
"""

from __future__ import annotations

from tantu.errors import Span, ParseError, P_UNEXPECTED_TOKEN, P_UNEXPECTED_EOF, P_TOO_DEEP
from tantu.tokens import Token, TokenType
from tantu import ast_nodes as A

_TT = TokenType

# Maximum nesting depth for expressions, patterns, and types. The checker and
# compiler use ~3-5 Python stack frames per AST level, so 100 keeps every later
# phase comfortably under CPython's default recursion limit.
_MAX_DEPTH = 100

# Maximum operands in a single flat operator chain (`a + b + c + ...`). These parse
# iteratively (no parser recursion), but build a left-nested BinOp list the checker and
# compiler later walk recursively; cap the chain so a pathologically long one is a clean
# P003 rather than a RecursionError. cli.main() raises the recursion limit so this cap
# sits well inside the guaranteed-safe region (see DECISIONS.md).
_MAX_CHAIN = 10_000

# Layout tokens carry no lexeme; name them plainly in diagnostics (W20).
_LAYOUT_NAMES = {
    _TT.NEWLINE: "end of line",
    _TT.INDENT: "indentation",
    _TT.DEDENT: "end of block",
}

_ADD_OPS = {_TT.PLUS: A.BinOpKind.ADD, _TT.MINUS: A.BinOpKind.SUB}
_MUL_OPS = {_TT.STAR: A.BinOpKind.MUL, _TT.SLASH: A.BinOpKind.DIV, _TT.PERCENT: A.BinOpKind.MOD}
_CMP_OPS = {
    _TT.EQEQ: A.BinOpKind.EQ, _TT.NE: A.BinOpKind.NE, _TT.LT: A.BinOpKind.LT,
    _TT.LE: A.BinOpKind.LE, _TT.GT: A.BinOpKind.GT, _TT.GE: A.BinOpKind.GE,
}


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.depth = 0

    # -- token cursor --------------------------------------------------------

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def peek2(self) -> Token:
        return self.tokens[min(self.pos + 1, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.type is not _TT.EOF:
            self.pos += 1
        return tok

    def check(self, tt: TokenType) -> bool:
        return self.tokens[self.pos].type is tt

    def match(self, tt: TokenType) -> bool:
        if self.check(tt):
            self.advance()
            return True
        return False

    def at_end(self) -> bool:
        return self.tokens[self.pos].type is _TT.EOF

    def expect(self, tt: TokenType, what: str) -> Token:
        if self.check(tt):
            return self.advance()
        self._unexpected(self.peek(), what)

    def _deeper(self) -> None:
        """Bump the nesting depth; raise P_TOO_DEEP past _MAX_DEPTH.

        Every recursive entry point (expression, _not, _unary, _pattern, _type)
        calls this on entry and decrements self.depth in a finally block, so the
        count stays correct even when a ParseError propagates.
        """
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise ParseError(P_TOO_DEEP, "expression nesting too deep", self.peek().span)

    def _unexpected(self, tok: Token, expected: str) -> None:
        if tok.type is _TT.EOF:
            raise ParseError(P_UNEXPECTED_EOF,
                             f"unexpected end of input, expected {expected}", tok.span)
        friendly = _LAYOUT_NAMES.get(tok.type)
        if friendly is not None:            # layout tokens have no lexeme: name them plainly
            raise ParseError(P_UNEXPECTED_TOKEN,
                             f"unexpected {friendly}, expected {expected}", tok.span)
        what = tok.lexeme if tok.lexeme else tok.type.name
        raise ParseError(P_UNEXPECTED_TOKEN,
                         f"unexpected {what!r}, expected {expected}", tok.span)

    # -- module / statements -------------------------------------------------

    def parse(self) -> A.Module:
        decls: list[A.Stmt] = []
        while not self.at_end():
            if self.check(_TT.NEWLINE):
                self.advance()
                continue
            if self.check(_TT.FN):          # named fn: a top-level declaration only
                decls.append(self.fn_decl())
                continue
            if self.check(_TT.ENUM):        # enum: a top-level declaration only
                decls.append(self.enum_decl())
                continue
            if self.check(_TT.STRUCT):      # struct: a top-level declaration only
                decls.append(self.struct_decl())
                continue
            if self.check(_TT.IMPORT):      # import: a top-level declaration only
                decls.append(self.import_decl())
                continue
            decls.append(self.statement())
        span = A.Span.merge(decls[0].span, decls[-1].span) if decls else self.peek().span
        return A.Module(span, decls)

    def statement(self) -> A.Stmt:
        t = self.peek().type
        if t is _TT.IF:
            return self.if_statement()
        if t is _TT.WHILE:
            return self.while_statement()
        if t is _TT.LOOP:
            return self.loop_statement()
        if t is _TT.FOR:
            return self.for_statement()
        if t is _TT.MATCH:                    # match as a statement: value discarded
            m = self._match()
            return A.ExprStmt(m.span, m)
        if t is _TT.SELECT:                   # select as a statement: value discarded
            s = self._select()
            return A.ExprStmt(s.span, s)
        return self.simple_statement()

    def simple_statement(self) -> A.Stmt:
        """A non-compound statement, consuming its terminating NEWLINE."""
        tok = self.peek()
        t = tok.type
        if t is _TT.LET:
            node = self._binding(mutable=False)
        elif t is _TT.VAR:
            node = self._binding(mutable=True)
        elif t is _TT.BREAK:
            self.advance()
            node = A.Break(tok.span)
        elif t is _TT.CONTINUE:
            self.advance()
            node = A.Continue(tok.span)
        elif t is _TT.RETURN:
            node = self._return()
        elif t is _TT.SPAWN:
            kw = self.advance()
            target = self.expression()          # a call or a lambda (checker validates)
            node = A.Spawn(Span.merge(kw.span, target.span), target)
        elif t is _TT.IDENT and self.peek2().type is _TT.EQ:
            node = self._assign()
        else:
            expr = self.expression()
            node = A.ExprStmt(expr.span, expr)
        # A statement ends at a NEWLINE, or implicitly at the DEDENT that closed a trailing
        # block-form RHS (block-body lambda / block-form if — §2.11 parsing notes).
        if self.tokens[self.pos - 1].type is not _TT.DEDENT:
            self.expect(_TT.NEWLINE, "end of statement")
        return node

    def _binding(self, *, mutable: bool):
        kw = self.advance()  # LET or VAR
        if not mutable and self.check(_TT.LPAREN):
            return self._let_tuple(kw)
        name = self.expect(_TT.IDENT, "a binding name")
        annotation = None
        if self.match(_TT.COLON):
            annotation = self._type()
        self.expect(_TT.EQ, "'='")
        value = self.expression()
        return A.Let(Span.merge(kw.span, value.span), name.lexeme, annotation, value, mutable)

    def _let_tuple(self, kw: Token) -> A.LetTuple:
        self.expect(_TT.LPAREN, "'('")
        names = [self.expect(_TT.IDENT, "a name").lexeme]
        self.expect(_TT.COMMA, "',' (tuple destructuring needs at least two names)")
        names.append(self.expect(_TT.IDENT, "a name").lexeme)
        while self.match(_TT.COMMA):
            names.append(self.expect(_TT.IDENT, "a name").lexeme)
        self.expect(_TT.RPAREN, "')'")
        annotation = self._type() if self.match(_TT.COLON) else None
        self.expect(_TT.EQ, "'='")
        value = self.expression()
        return A.LetTuple(Span.merge(kw.span, value.span), names, annotation, value)

    def struct_decl(self) -> A.StructDecl:
        kw = self.advance()  # STRUCT
        name = self.expect(_TT.IDENT, "a struct name")
        tps = self._type_params()
        self.expect(_TT.COLON, "':'")
        self.expect(_TT.NEWLINE, "a newline")
        self.expect(_TT.INDENT, "an indented list of fields")
        fields: list[A.FieldDecl] = []
        while not self.check(_TT.DEDENT):
            if self.at_end():
                self._unexpected(self.peek(), "a field")
            fields.append(self._field())
        self.expect(_TT.DEDENT, "a dedent")
        return A.StructDecl(Span.merge(kw.span, name.span), name.lexeme, fields, type_params=tps)

    def _field(self) -> A.FieldDecl:
        name = self.expect(_TT.IDENT, "a field name")
        self.expect(_TT.COLON, "':'")
        ann = self._type()
        self.expect(_TT.NEWLINE, "a newline")
        return A.FieldDecl(Span.merge(name.span, ann.span), name.lexeme, ann)

    def for_statement(self) -> A.For:
        kw = self.advance()  # FOR
        var = self.expect(_TT.IDENT, "a loop variable")
        self.expect(_TT.IN, "'in'")
        it = self.expression()
        self.expect(_TT.COLON, "':'")
        body = self._block()
        return A.For(Span.merge(kw.span, it.span), var.lexeme, it, body)

    def _assign(self) -> A.Assign:
        name = self.advance()  # IDENT
        self.expect(_TT.EQ, "'='")
        value = self.expression()
        return A.Assign(Span.merge(name.span, value.span), name.lexeme, value)

    def if_statement(self) -> A.Stmt:
        """An `if` in statement position: an if-STATEMENT, or (§2.8) an inline
        if-EXPRESSION whose value is discarded / yielded as the block tail.

        Disambiguation is LL with one committed expression — no backtracking. After
        `if cond:` an inline branch that can only be a statement (let/var/return/
        break/continue/spawn, or `name = ...` assignment) is a statement `if`. Any
        other inline branch parses as ONE expression; if the very next token is a
        same-line `elif`/`else`, that expression was the first branch of an
        if-EXPRESSION and we commit to that form; a NEWLINE instead means it was a
        one-statement inline block of a normal if-STATEMENT.
        """
        kw = self.advance()  # IF
        cond = self.expression()
        self.expect(_TT.COLON, "':'")
        t = self.peek().type
        stmt_head = t in (_TT.LET, _TT.VAR, _TT.RETURN, _TT.BREAK, _TT.CONTINUE, _TT.SPAWN) \
            or (t is _TT.IDENT and self.peek2().type is _TT.EQ)
        if t is not _TT.NEWLINE and not stmt_head:
            e = self.expression()
            after = self.peek().type
            if after in (_TT.ELIF, _TT.ELSE) and self.tokens[self.pos - 1].type is not _TT.DEDENT:
                # same-line elif/else after the branch expression → if-EXPRESSION (§2.8)
                node = self._if_expr_tail(kw, [(cond, [A.ExprStmt(e.span, e)])])
                if self.tokens[self.pos - 1].type is not _TT.DEDENT:
                    self.expect(_TT.NEWLINE, "end of statement")
                return A.ExprStmt(node.span, node)
            # NEWLINE (or the DEDENT of a trailing block-form expr) → statement if
            # with a one-statement inline block; same termination rule as simple_statement.
            if self.tokens[self.pos - 1].type is not _TT.DEDENT:
                self.expect(_TT.NEWLINE, "end of statement")
            first_body: list[A.Stmt] = [A.ExprStmt(e.span, e)]
        else:
            first_body = self._block()
        branches: list = [(cond, first_body)]
        while self.check(_TT.ELIF):
            self.advance()
            c = self.expression()
            self.expect(_TT.COLON, "':'")
            branches.append((c, self._block()))
        orelse = None
        if self.check(_TT.ELSE):
            self.advance()
            self.expect(_TT.COLON, "':'")
            orelse = self._block()
        return A.If(Span.merge(kw.span, cond.span), branches, orelse)

    def while_statement(self) -> A.While:
        kw = self.advance()
        cond = self.expression()
        self.expect(_TT.COLON, "':'")
        body = self._block()
        return A.While(Span.merge(kw.span, cond.span), cond, body)

    def loop_statement(self) -> A.Loop:
        kw = self.advance()
        self.expect(_TT.COLON, "':'")
        body = self._block()
        return A.Loop(kw.span, body)

    def _return(self) -> A.Return:
        kw = self.advance()  # RETURN
        value = None if self.check(_TT.NEWLINE) else self.expression()
        span = Span.merge(kw.span, value.span) if value is not None else kw.span
        return A.Return(span, value)

    # -- functions & lambdas -------------------------------------------------

    def fn_decl(self) -> A.FnDecl:
        kw = self.advance()  # FN
        name = self.expect(_TT.IDENT, "a function name")
        tps = self._type_params()
        self.expect(_TT.LPAREN, "'('")
        params = self._params()
        self.expect(_TT.RPAREN, "')'")
        ret = self._type() if self.match(_TT.ARROW) else None
        self.expect(_TT.COLON, "':'")
        body = self._block()
        return A.FnDecl(Span.merge(kw.span, name.span), name.lexeme, params, ret, body,
                        type_params=tps)

    def _type_params(self) -> list[str]:
        """An optional generic parameter list `[T, U, ...]` (M9b). Empty `[]` is a parse error."""
        if not self.match(_TT.LBRACKET):
            return []
        names = [self.expect(_TT.IDENT, "a type parameter").lexeme]
        while self.match(_TT.COMMA):
            names.append(self.expect(_TT.IDENT, "a type parameter").lexeme)
        self.expect(_TT.RBRACKET, "']'")
        return names

    def _lambda(self) -> A.Lambda:
        kw = self.advance()  # FN
        self.expect(_TT.LPAREN, "'('")
        params = self._params()
        self.expect(_TT.RPAREN, "')'")
        ret = self._type() if self.match(_TT.ARROW) else None
        self.expect(_TT.COLON, "':'")
        if self.check(_TT.NEWLINE):
            body = self._block()                       # block body
        else:
            e = self.expression()                      # inline expression body (block value)
            body = [A.ExprStmt(e.span, e)]
        end = body[-1].span if body else kw.span
        return A.Lambda(Span.merge(kw.span, end), params, ret, body)

    def _params(self) -> list[A.Param]:
        params: list[A.Param] = []
        if not self.check(_TT.RPAREN):
            params.append(self._param())
            while self.match(_TT.COMMA):
                params.append(self._param())
        return params

    def _param(self) -> A.Param:
        name = self.expect(_TT.IDENT, "a parameter name")
        self.expect(_TT.COLON, "':' (parameters require a type)")
        ann = self._type()
        return A.Param(Span.merge(name.span, ann.span), name.lexeme, ann)

    def _block(self) -> list[A.Stmt]:
        """A block: NEWLINE INDENT stmt+ DEDENT, or a single inline simple statement."""
        if self.check(_TT.NEWLINE):
            self.advance()
            self.expect(_TT.INDENT, "an indented block")
            stmts: list[A.Stmt] = []
            while not self.check(_TT.DEDENT):
                if self.at_end():
                    self._unexpected(self.peek(), "a dedent")
                stmts.append(self.statement())
            self.expect(_TT.DEDENT, "a dedent")
            return stmts
        return [self.simple_statement()]

    # -- expressions ---------------------------------------------------------

    def expression(self) -> A.Expr:
        self._deeper()
        try:
            if self.check(_TT.IF):
                return self._if_expr()
            if self.check(_TT.FN):
                return self._lambda()
            if self.check(_TT.MATCH):
                return self._match()
            if self.check(_TT.SELECT):
                return self._select()
            return self._or()
        finally:
            self.depth -= 1

    # -- enums, match, patterns ----------------------------------------------

    def import_decl(self) -> A.Import:
        kw = self.advance()  # IMPORT
        name = self.expect(_TT.IDENT, "a module name")
        self.expect(_TT.NEWLINE, "a newline")
        return A.Import(Span.merge(kw.span, name.span), name.lexeme)

    def enum_decl(self) -> A.EnumDecl:
        kw = self.advance()  # ENUM
        name = self.expect(_TT.IDENT, "an enum name")
        tps = self._type_params()
        self.expect(_TT.COLON, "':'")
        self.expect(_TT.NEWLINE, "a newline")
        self.expect(_TT.INDENT, "an indented list of variants")
        variants: list[A.VariantDecl] = []
        while not self.check(_TT.DEDENT):
            if self.at_end():
                self._unexpected(self.peek(), "a variant")
            variants.append(self._variant())
        self.expect(_TT.DEDENT, "a dedent")
        return A.EnumDecl(Span.merge(kw.span, name.span), name.lexeme, variants, type_params=tps)

    def _variant(self) -> A.VariantDecl:
        name = self.expect(_TT.IDENT, "a variant name")
        payload: list[A.TypeExpr] = []
        if self.match(_TT.LPAREN):
            payload.append(self._type())
            while self.match(_TT.COMMA):
                payload.append(self._type())
            self.expect(_TT.RPAREN, "')'")
        self.expect(_TT.NEWLINE, "a newline")
        return A.VariantDecl(name.span, name.lexeme, payload)

    def _match(self) -> A.Match:
        kw = self.advance()  # MATCH
        scrut = self.expression()
        self.expect(_TT.COLON, "':'")
        self.expect(_TT.NEWLINE, "a newline")
        self.expect(_TT.INDENT, "indented match arms")
        arms: list[A.MatchArm] = []
        while not self.check(_TT.DEDENT):
            if self.at_end():
                self._unexpected(self.peek(), "a match arm")
            arms.append(self._arm())
        self.expect(_TT.DEDENT, "a dedent")
        return A.Match(Span.merge(kw.span, scrut.span), scrut, arms)

    def _arm(self) -> A.MatchArm:
        pat = self._pattern()
        body = self._arm_tail()
        return A.MatchArm(pat.span, pat, body)

    def _arm_tail(self) -> list[A.Stmt]:
        """Parse `=> <body>` where body is an indented block or a single inline statement."""
        self.expect(_TT.FATARROW, "'=>'")
        if self.check(_TT.NEWLINE):
            self.advance()
            self.expect(_TT.INDENT, "an indented arm body")
            body: list[A.Stmt] = []
            while not self.check(_TT.DEDENT):
                body.append(self.statement())
            self.expect(_TT.DEDENT, "a dedent")
            return body
        return [self.simple_statement()]      # inline arm body (consumes its NEWLINE)

    # -- select (M9a) --------------------------------------------------------

    def _select(self) -> A.Select:
        kw = self.advance()  # SELECT
        self.expect(_TT.COLON, "':'")
        self.expect(_TT.NEWLINE, "a newline")
        self.expect(_TT.INDENT, "indented select arms")
        arms: list[A.SelectArm] = []
        while not self.check(_TT.DEDENT):
            if self.at_end():
                self._unexpected(self.peek(), "a select arm")
            arms.append(self._select_arm())
        self.expect(_TT.DEDENT, "a dedent")
        return A.Select(kw.span, arms)

    def _select_arm(self) -> A.SelectArm:
        tok = self.peek()
        if tok.type is _TT.ELSE:                                  # else => ...
            self.advance()
            return A.SelectArm(tok.span, "else", None, None, None, self._arm_tail())
        if tok.type is _TT.IDENT and self.peek2().type is _TT.EQ:  # binder = recv(ch) => ...
            self.advance()                                        # binder
            self.expect(_TT.EQ, "'='")
            binder = None if tok.lexeme == "_" else tok.lexeme
            return self._recv_arm(tok.span, binder)
        if tok.type is _TT.IDENT and tok.lexeme == "recv":        # recv(ch) => ...  (discard)
            return self._recv_arm(tok.span, None)
        if tok.type is _TT.IDENT and tok.lexeme == "send":        # send(ch, e) => ...
            return self._send_arm(tok.span)
        self._unexpected(tok, "a select arm ('recv', 'send', or 'else')")

    def _recv_arm(self, start: Span, binder: str | None) -> A.SelectArm:
        name = self.expect(_TT.IDENT, "'recv'")
        if name.lexeme != "recv":
            self._unexpected(name, "'recv'")
        self.expect(_TT.LPAREN, "'('")
        ch = self.expression()
        rp = self.expect(_TT.RPAREN, "')'")
        return A.SelectArm(Span.merge(start, rp.span), "recv", binder, ch, None, self._arm_tail())

    def _send_arm(self, start: Span) -> A.SelectArm:
        self.advance()  # 'send' IDENT
        self.expect(_TT.LPAREN, "'('")
        ch = self.expression()
        self.expect(_TT.COMMA, "','")
        val = self.expression()
        rp = self.expect(_TT.RPAREN, "')'")
        return A.SelectArm(Span.merge(start, rp.span), "send", None, ch, val, self._arm_tail())

    def _pattern(self) -> A.Pattern:
        self._deeper()
        try:
            tok = self.peek()
            t = tok.type
            if t is _TT.MINUS or t is _TT.INT:
                neg = self.match(_TT.MINUS)
                num = self.expect(_TT.INT, "an integer literal pattern")
                v = -num.value if neg else num.value
                return A.PLiteral(Span.merge(tok.span, num.span), v, "int")
            if t is _TT.STR:
                self.advance()
                return A.PLiteral(tok.span, tok.value, "str")
            if t is _TT.TRUE:
                self.advance()
                return A.PLiteral(tok.span, True, "bool")
            if t is _TT.FALSE:
                self.advance()
                return A.PLiteral(tok.span, False, "bool")
            if t is _TT.FLOAT:
                self._unexpected(tok, "a pattern (Float literal patterns are not allowed — "
                                      "match a Float with a binding and comparisons)")
            if t is _TT.IDENT:
                name = self.advance()
                if name.lexeme == "_":
                    return A.PWildcard(name.span)
                if self.match(_TT.LPAREN):
                    args = [self._pattern()]
                    while self.match(_TT.COMMA):
                        args.append(self._pattern())
                    rp = self.expect(_TT.RPAREN, "')'")
                    return A.PVariant(Span.merge(name.span, rp.span), name.lexeme, args)
                return A.PIdent(name.span, name.lexeme)
            if t is _TT.LPAREN:
                lp = self.advance()
                first = self._pattern()
                if self.match(_TT.COMMA):
                    elems = [first, self._pattern()]
                    while self.match(_TT.COMMA):
                        elems.append(self._pattern())
                    rp = self.expect(_TT.RPAREN, "')'")
                    return A.PTuple(Span.merge(lp.span, rp.span), elems)
                self.expect(_TT.RPAREN, "')'")
                return first  # grouped pattern
            self._unexpected(tok, "a pattern")
        finally:
            self.depth -= 1

    def _if_expr(self) -> A.Expr:
        kw = self.advance()  # IF
        cond = self.expression()
        self.expect(_TT.COLON, "':'")
        return self._if_expr_tail(kw, [(cond, self._if_branch())])

    def _if_expr_tail(self, kw: Token, branches: list) -> A.IfExpr:
        """The `{elif}* else` tail of an if-expression (first branch already parsed)."""
        while self.check(_TT.ELIF):
            self.advance()
            c = self.expression()
            self.expect(_TT.COLON, "':'")
            branches.append((c, self._if_branch()))
        self.expect(_TT.ELSE, "'else' (an if-expression must have an else)")
        self.expect(_TT.COLON, "':'")
        orelse = self._if_branch()
        return A.IfExpr(Span.merge(kw.span, orelse[-1].span), branches, orelse)

    def _if_branch(self) -> list[A.Stmt]:
        """One if-expression branch body: an indented block, or an inline expression (§2.6)."""
        if self.check(_TT.NEWLINE):
            return self._block()
        e = self.expression()
        return [A.ExprStmt(e.span, e)]

    def _or(self) -> A.Expr:
        left = self._and()
        n = 1
        while self.check(_TT.OR):
            n += 1
            if n > _MAX_CHAIN:
                raise ParseError(P_TOO_DEEP, "expression too long", self.peek().span)
            self.advance()
            right = self._and()
            left = A.Logical(Span.merge(left.span, right.span), A.LogicalKind.OR, left, right)
        return left

    def _and(self) -> A.Expr:
        left = self._not()
        n = 1
        while self.check(_TT.AND):
            n += 1
            if n > _MAX_CHAIN:
                raise ParseError(P_TOO_DEEP, "expression too long", self.peek().span)
            self.advance()
            right = self._not()
            left = A.Logical(Span.merge(left.span, right.span), A.LogicalKind.AND, left, right)
        return left

    def _not(self) -> A.Expr:
        self._deeper()
        try:
            if self.check(_TT.NOT):
                tok = self.advance()
                operand = self._not()
                return A.Unary(Span.merge(tok.span, operand.span), A.UnOpKind.NOT, operand)
            return self._cmp()
        finally:
            self.depth -= 1

    def _cmp(self) -> A.Expr:
        left = self._add()
        op = _CMP_OPS.get(self.peek().type)
        if op is not None:
            self.advance()
            right = self._add()
            if self.peek().type in _CMP_OPS:  # non-associative
                self._unexpected(self.peek(),
                                 "a newline (comparisons are non-associative; use parentheses)")
            return A.BinOp(Span.merge(left.span, right.span), op, left, right)
        return left

    def _add(self) -> A.Expr:
        left = self._mul()
        n = 1
        while self.peek().type in _ADD_OPS:
            n += 1
            if n > _MAX_CHAIN:
                raise ParseError(P_TOO_DEEP, "expression too long", self.peek().span)
            op = _ADD_OPS[self.advance().type]
            right = self._mul()
            left = A.BinOp(Span.merge(left.span, right.span), op, left, right)
        return left

    def _mul(self) -> A.Expr:
        left = self._unary()
        n = 1
        while self.peek().type in _MUL_OPS:
            n += 1
            if n > _MAX_CHAIN:
                raise ParseError(P_TOO_DEEP, "expression too long", self.peek().span)
            op = _MUL_OPS[self.advance().type]
            right = self._unary()
            left = A.BinOp(Span.merge(left.span, right.span), op, left, right)
        return left

    def _unary(self) -> A.Expr:
        self._deeper()
        try:
            if self.check(_TT.MINUS):
                optok = self.advance()
                operand = self._unary()
                return A.Unary(Span.merge(optok.span, operand.span), A.UnOpKind.NEG, operand)
            return self._postfix()
        finally:
            self.depth -= 1

    def _postfix(self) -> A.Expr:
        expr = self._primary()
        while True:
            if self.check(_TT.LPAREN):
                expr = self._finish_call(expr)
            elif self.check(_TT.LBRACKET):
                self.advance()
                idx = self.expression()
                rb = self.expect(_TT.RBRACKET, "']'")
                expr = A.Index(Span.merge(expr.span, rb.span), expr, idx)
            elif self.check(_TT.DOT):
                self.advance()
                name = self.expect(_TT.IDENT, "a field name")
                expr = A.Field(Span.merge(expr.span, name.span), expr, name.lexeme)
            elif self.check(_TT.QUESTION):
                q = self.advance()
                expr = A.Try(Span.merge(expr.span, q.span), expr)
            else:
                return expr

    def _finish_call(self, callee: A.Expr) -> A.Expr:
        self.expect(_TT.LPAREN, "'('")
        args: list[A.Expr] = []
        if not self.check(_TT.RPAREN):
            args.append(self.expression())
            while self.match(_TT.COMMA):
                args.append(self.expression())
        rparen = self.expect(_TT.RPAREN, "')'")
        return A.Call(Span.merge(callee.span, rparen.span), callee, args)

    def _primary(self) -> A.Expr:
        tok = self.peek()
        t = tok.type
        if t is _TT.INT:
            self.advance()
            return A.IntLit(tok.span, tok.value)
        if t is _TT.FLOAT:
            self.advance()
            return A.FloatLit(tok.span, tok.value)
        if t is _TT.STR:
            self.advance()
            return A.StrLit(tok.span, tok.value)
        if t is _TT.TRUE:
            self.advance()
            return A.BoolLit(tok.span, True)
        if t is _TT.FALSE:
            self.advance()
            return A.BoolLit(tok.span, False)
        if t is _TT.IDENT and tok.lexeme == "chan" and self.peek2().type is _TT.LBRACKET:
            self.advance()                      # 'chan' (a special-cased primary, §2.2)
            self.expect(_TT.LBRACKET, "'['")
            elem = self._type()
            self.expect(_TT.RBRACKET, "']'")
            self.expect(_TT.LPAREN, "'('")
            cap = None if self.check(_TT.RPAREN) else self.expression()
            rp = self.expect(_TT.RPAREN, "')'")
            return A.ChanNew(Span.merge(tok.span, rp.span), elem, cap)
        if t is _TT.IDENT:
            self.advance()
            return A.Name(tok.span, tok.lexeme)
        if t is _TT.LBRACKET:
            lb = self.advance()
            elems: list[A.Expr] = []
            if not self.check(_TT.RBRACKET):
                elems.append(self.expression())
                while self.match(_TT.COMMA):
                    if self.check(_TT.RBRACKET):
                        break
                    elems.append(self.expression())
            rb = self.expect(_TT.RBRACKET, "']'")
            return A.ListLit(Span.merge(lb.span, rb.span), elems)
        if t is _TT.LPAREN:
            lp = self.advance()
            if self.check(_TT.RPAREN):               # unit literal `()` (§2.11 primary)
                rp = self.advance()
                return A.UnitLit(Span.merge(lp.span, rp.span))
            first = self.expression()
            if self.match(_TT.COMMA):                # tuple (arity >= 2; no 1-tuples)
                elems = [first, self.expression()]
                while self.match(_TT.COMMA):
                    if self.check(_TT.RPAREN):
                        break
                    elems.append(self.expression())
                rp = self.expect(_TT.RPAREN, "')'")
                return A.TupleLit(Span.merge(lp.span, rp.span), elems)
            self.expect(_TT.RPAREN, "')'")
            return first  # grouping is transparent
        self._unexpected(tok, "an expression")

    # -- types ---------------------------------------------------------------

    def _type(self) -> A.TypeExpr:
        # §2.11 `type`: function type `(T, ...) -> R`, or IDENT with optional [args].
        # Tuple types `(T, T)` arrive in M5.
        self._deeper()
        try:
            if self.check(_TT.LPAREN):
                return self._fn_type()
            name = self.expect(_TT.IDENT, "a type name")
            if self.match(_TT.LBRACKET):
                args = [self._type()]
                while self.match(_TT.COMMA):
                    args.append(self._type())
                rb = self.expect(_TT.RBRACKET, "']'")
                return A.NamedType(Span.merge(name.span, rb.span), name.lexeme, args)
            return A.NamedType(name.span, name.lexeme, [])
        finally:
            self.depth -= 1

    def _fn_type(self) -> A.TypeExpr:
        lp = self.advance()  # LPAREN
        parts: list[A.TypeExpr] = []
        if not self.check(_TT.RPAREN):
            parts.append(self._type())
            while self.match(_TT.COMMA):
                parts.append(self._type())
        rp = self.expect(_TT.RPAREN, "')'")
        if self.match(_TT.ARROW):                    # function type
            ret = self._type()
            return A.FnTypeExpr(Span.merge(lp.span, ret.span), parts, ret)
        if not parts:                                # `()` alone is not a type (only `() -> R` is)
            raise ParseError(P_UNEXPECTED_TOKEN,
                             "expected a type; for the unit type write 'Unit'",
                             Span.merge(lp.span, rp.span))
        if len(parts) == 1:
            return parts[0]                          # parenthesized single type
        return A.TupleTypeExpr(Span.merge(lp.span, rp.span), parts)   # tuple type (arity >= 2)


def parse(tokens: list[Token]) -> A.Module:
    """Parse a token list into a Module AST. Raises ParseError on malformed input."""
    return _Parser(tokens).parse()
