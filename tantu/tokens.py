"""Token types and the Token record. No logic (DESIGN.md §6.1).

The full v1 TokenType set is defined now (DESIGN.md §2.2) even though M1's lexer emits only
a subset — same append-only discipline as the Op enum, so later milestones never renumber.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from tantu.errors import Span


class TokenType(enum.Enum):
    # literals
    INT = enum.auto()
    FLOAT = enum.auto()
    STR = enum.auto()
    IDENT = enum.auto()

    # keywords (DESIGN.md §2.2)
    FN = enum.auto()
    LET = enum.auto()
    VAR = enum.auto()
    IF = enum.auto()
    ELIF = enum.auto()
    ELSE = enum.auto()
    WHILE = enum.auto()
    FOR = enum.auto()
    IN = enum.auto()
    LOOP = enum.auto()
    BREAK = enum.auto()
    CONTINUE = enum.auto()
    RETURN = enum.auto()
    MATCH = enum.auto()
    ENUM = enum.auto()
    STRUCT = enum.auto()
    SPAWN = enum.auto()
    SELECT = enum.auto()
    IMPORT = enum.auto()
    AND = enum.auto()
    OR = enum.auto()
    NOT = enum.auto()
    TRUE = enum.auto()
    FALSE = enum.auto()

    # operators & punctuation
    ARROW = enum.auto()       # ->
    FATARROW = enum.auto()    # =>
    COLON = enum.auto()       # :
    COMMA = enum.auto()       # ,
    DOT = enum.auto()         # .
    LPAREN = enum.auto()      # (
    RPAREN = enum.auto()      # )
    LBRACKET = enum.auto()    # [
    RBRACKET = enum.auto()    # ]
    QUESTION = enum.auto()    # ?
    EQ = enum.auto()          # =
    EQEQ = enum.auto()        # ==
    NE = enum.auto()          # !=
    LT = enum.auto()          # <
    LE = enum.auto()          # <=
    GT = enum.auto()          # >
    GE = enum.auto()          # >=
    PLUS = enum.auto()        # +
    MINUS = enum.auto()       # -
    STAR = enum.auto()        # *
    SLASH = enum.auto()       # /
    PERCENT = enum.auto()     # %

    # layout
    NEWLINE = enum.auto()
    INDENT = enum.auto()
    DEDENT = enum.auto()
    EOF = enum.auto()

    def __repr__(self) -> str:  # readable in test diffs
        return self.name


# keyword spelling -> TokenType (recognized in the lexer from day one, DESIGN.md §2.2)
KEYWORDS: dict[str, TokenType] = {
    "fn": TokenType.FN, "let": TokenType.LET, "var": TokenType.VAR,
    "if": TokenType.IF, "elif": TokenType.ELIF, "else": TokenType.ELSE,
    "while": TokenType.WHILE, "for": TokenType.FOR, "in": TokenType.IN,
    "loop": TokenType.LOOP, "break": TokenType.BREAK, "continue": TokenType.CONTINUE,
    "return": TokenType.RETURN, "match": TokenType.MATCH, "enum": TokenType.ENUM,
    "struct": TokenType.STRUCT, "spawn": TokenType.SPAWN, "select": TokenType.SELECT,
    "import": TokenType.IMPORT,
    "and": TokenType.AND, "or": TokenType.OR, "not": TokenType.NOT,
    "true": TokenType.TRUE, "false": TokenType.FALSE,
}


@dataclass(frozen=True, slots=True)
class Token:
    """A lexeme with its decoded value.

    `value` is the decoded literal for INT/FLOAT/STR (underscores stripped, escapes applied),
    else None. `lexeme` is the raw source text.
    """

    type: TokenType
    lexeme: str
    value: int | float | str | None
    span: Span

    def __repr__(self) -> str:
        if self.value is not None:
            return f"{self.type.name}({self.value!r})"
        return f"{self.type.name}({self.lexeme!r})" if self.lexeme else self.type.name
