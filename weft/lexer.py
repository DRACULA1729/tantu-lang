"""Hand-written lexer with an indentation layer (DESIGN.md §2.1, §6.2).

Produces a token stream ending in `[..., NEWLINE?, DEDENT*, EOF]`. Indentation is
spaces-only (a tab in indentation is a hard error); INDENT/DEDENT are synthesized from an
indent stack; blank and comment-only lines emit no layout tokens; newlines and indentation
are ignored inside unclosed `(`/`[` (implicit line joining via a bracket-depth counter).

The full machinery is present from M1 even though M1 programs are effectively single-line —
M2 then needs no lexer change (DESIGN.md M1/M2, Fable ruling 5).
"""

from __future__ import annotations

from weft.errors import (
    Span, LexError,
    L_TABS_IN_INDENT, L_BAD_DEDENT, L_ILLEGAL_CHAR, L_INVALID_ESCAPE, L_UNTERMINATED_STRING,
)
from weft.tokens import Token, TokenType, KEYWORDS

_TT = TokenType

_TWO_CHAR = {
    "->": _TT.ARROW, "=>": _TT.FATARROW, "==": _TT.EQEQ, "!=": _TT.NE,
    "<=": _TT.LE, ">=": _TT.GE,
}
_ONE_CHAR = {
    ":": _TT.COLON, ",": _TT.COMMA, ".": _TT.DOT, "(": _TT.LPAREN, ")": _TT.RPAREN,
    "[": _TT.LBRACKET, "]": _TT.RBRACKET, "?": _TT.QUESTION, "=": _TT.EQ,
    "<": _TT.LT, ">": _TT.GT, "+": _TT.PLUS, "-": _TT.MINUS, "*": _TT.STAR,
    "/": _TT.SLASH, "%": _TT.PERCENT,
}
_OPEN_BRACKETS = {"(", "["}
_CLOSE_BRACKETS = {")", "]"}
_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}
_LAYOUT = (_TT.NEWLINE, _TT.INDENT, _TT.DEDENT)


def _is_ident_start(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_"


def _is_ident_cont(c: str) -> bool:
    return _is_ident_start(c) or ("0" <= c <= "9")


class _Lexer:
    def __init__(self, src: str, file: str) -> None:
        self.src = src
        self.file = file
        self.n = len(src)
        self.i = 0
        self.line = 1
        self.line_start = 0
        self.indents = [0]
        self.bracket_depth = 0
        self.at_line_start = True
        self.tokens: list[Token] = []

    # -- span/emit helpers ---------------------------------------------------

    def _span(self, start: int, end: int) -> Span:
        return Span(self.file, start, end, self.line, start - self.line_start)

    def _emit(self, tt: TokenType, start: int, end: int, value: object = None) -> None:
        self.tokens.append(Token(tt, self.src[start:end], value, self._span(start, end)))

    def _emit_layout(self, tt: TokenType, at: int) -> None:
        self.tokens.append(Token(tt, "", None, self._span(at, at)))

    # -- driver --------------------------------------------------------------

    def run(self) -> list[Token]:
        while self.i < self.n:
            if self.at_line_start and self.bracket_depth == 0:
                self._handle_line_start()
                continue
            c = self.src[self.i]
            if c == "\n":
                self._newline()
            elif c == " " or c == "\t":
                self.i += 1
            elif c == "#":
                self._skip_comment()
            else:
                self._scan_token()
        self._finish()
        return self.tokens

    def _handle_line_start(self) -> None:
        start = self.i
        while self.i < self.n and self.src[self.i] == " ":
            self.i += 1
        if self.i < self.n and self.src[self.i] == "\t":
            raise LexError(L_TABS_IN_INDENT, "tabs are not allowed in indentation",
                           self._span(self.i, self.i))
        indent = self.i - start

        # blank or comment-only line: no layout tokens, no indent-stack change
        if self.i >= self.n or self.src[self.i] == "\n" or self.src[self.i] == "#":
            while self.i < self.n and self.src[self.i] != "\n":
                self.i += 1
            if self.i < self.n:  # consume the newline
                self.i += 1
                self.line += 1
                self.line_start = self.i
            self.at_line_start = True
            return

        if indent > self.indents[-1]:
            self.indents.append(indent)
            self._emit_layout(_TT.INDENT, self.i)
        elif indent < self.indents[-1]:
            while indent < self.indents[-1]:
                self.indents.pop()
                self._emit_layout(_TT.DEDENT, self.i)
            if indent != self.indents[-1]:
                raise LexError(L_BAD_DEDENT,
                               "unindent does not match any outer indentation level",
                               self._span(self.i, self.i))
        self.at_line_start = False

    def _newline(self) -> None:
        nl = self.i
        if (self.bracket_depth == 0 and self.tokens
                and self.tokens[-1].type not in _LAYOUT):
            self._emit(_TT.NEWLINE, nl, nl + 1)
        self.i += 1
        self.line += 1
        self.line_start = self.i
        if self.bracket_depth == 0:
            self.at_line_start = True

    def _skip_comment(self) -> None:
        while self.i < self.n and self.src[self.i] != "\n":
            self.i += 1

    # -- token scanners ------------------------------------------------------

    def _scan_token(self) -> None:
        self.at_line_start = False
        c = self.src[self.i]
        if "0" <= c <= "9":
            self._number()
        elif c == '"':
            self._string()
        elif _is_ident_start(c):
            self._ident()
        else:
            self._operator()

    def _number(self) -> None:
        start = self.i
        is_float = False
        self._consume_digits()
        # fraction: '.' immediately followed by a digit (so `1.` stays INT + DOT)
        if (self.i + 1 < self.n and self.src[self.i] == "."
                and "0" <= self.src[self.i + 1] <= "9"):
            is_float = True
            self.i += 1
            self._consume_digits()
        # exponent
        if self.i < self.n and self.src[self.i] in "eE":
            j = self.i + 1
            if j < self.n and self.src[j] in "+-":
                j += 1
            if j < self.n and "0" <= self.src[j] <= "9":
                is_float = True
                self.i = j
                self._consume_digits()
        text = self.src[start:self.i]
        cleaned = text.replace("_", "")
        if is_float:
            self._emit(_TT.FLOAT, start, self.i, float(cleaned))
        else:
            self._emit(_TT.INT, start, self.i, int(cleaned))

    def _consume_digits(self) -> None:
        while self.i < self.n and (("0" <= self.src[self.i] <= "9") or self.src[self.i] == "_"):
            self.i += 1

    def _string(self) -> None:
        start = self.i
        self.i += 1  # opening quote
        chars: list[str] = []
        while self.i < self.n:
            c = self.src[self.i]
            if c == '"':
                self.i += 1
                self._emit(_TT.STR, start, self.i, "".join(chars))
                return
            if c == "\n":
                break  # unterminated (strings are single-line in v1)
            if c == "\\":
                if self.i + 1 >= self.n:
                    break
                esc = self.src[self.i + 1]
                if esc in _STRING_ESCAPES:
                    chars.append(_STRING_ESCAPES[esc])
                    self.i += 2
                    continue
                raise LexError(L_INVALID_ESCAPE, f"invalid escape '\\{esc}'",
                               self._span(self.i, self.i + 2))
            chars.append(c)
            self.i += 1
        raise LexError(L_UNTERMINATED_STRING, "unterminated string literal",
                       self._span(start, min(self.i, self.n)))

    def _ident(self) -> None:
        start = self.i
        while self.i < self.n and _is_ident_cont(self.src[self.i]):
            self.i += 1
        text = self.src[start:self.i]
        kw = KEYWORDS.get(text)
        self._emit(kw if kw is not None else _TT.IDENT, start, self.i)

    def _operator(self) -> None:
        start = self.i
        two = self.src[self.i:self.i + 2]
        if two in _TWO_CHAR:
            self.i += 2
            self._emit(_TWO_CHAR[two], start, self.i)
            return
        c = self.src[self.i]
        tt = _ONE_CHAR.get(c)
        if tt is not None:
            self.i += 1
            if c in _OPEN_BRACKETS:
                self.bracket_depth += 1
            elif c in _CLOSE_BRACKETS and self.bracket_depth > 0:
                self.bracket_depth -= 1
            self._emit(tt, start, self.i)
            return
        raise LexError(L_ILLEGAL_CHAR, f"illegal character {c!r}", self._span(start, start + 1))

    def _finish(self) -> None:
        # Terminate a final logical line; skip if still inside brackets so an unclosed
        # bracket surfaces as "unexpected end of input" rather than a spurious NEWLINE.
        if self.bracket_depth == 0 and self.tokens and self.tokens[-1].type not in _LAYOUT:
            self._emit_layout(_TT.NEWLINE, self.n)
        while len(self.indents) > 1:
            self.indents.pop()
            self._emit_layout(_TT.DEDENT, self.n)
        self._emit_layout(_TT.EOF, self.n)


def tokenize(src: str, file: str = "<unknown>") -> list[Token]:
    """Lex `src` into a token list ending in EOF. Raises LexError on illegal input."""
    return _Lexer(src, file).run()
