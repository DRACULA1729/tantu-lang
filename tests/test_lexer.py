"""Lexer unit tests (DESIGN.md §8.1): token streams, layout, and error cases."""

from __future__ import annotations

import pytest

from tantu.lexer import tokenize
from tantu.tokens import TokenType as T
from tantu.errors import (
    LexError, L_TABS_IN_INDENT, L_BAD_DEDENT, L_ILLEGAL_CHAR, L_UNTERMINATED_STRING,
    L_INVALID_NUMBER,
)


def types(src: str) -> list[T]:
    return [t.type for t in tokenize(src, "<t>")]


def test_arithmetic_stream():
    assert types("1 + 2 * 3") == [T.INT, T.PLUS, T.INT, T.STAR, T.INT, T.NEWLINE, T.EOF]


def test_values_decoded():
    toks = tokenize('1_000 3.14 1e-3 "hi\\n"', "<t>")
    assert toks[0].value == 1000 and toks[0].type is T.INT
    assert toks[1].value == 3.14 and toks[1].type is T.FLOAT
    assert toks[2].value == 0.001 and toks[2].type is T.FLOAT
    assert toks[3].value == "hi\n" and toks[3].type is T.STR


def test_keyword_vs_ident():
    assert types("fn foo let") == [T.FN, T.IDENT, T.LET, T.NEWLINE, T.EOF]


def test_number_dot_is_not_float():
    # `1.foo` -> INT DOT IDENT (fraction needs a digit after '.')
    assert types("1.foo") == [T.INT, T.DOT, T.IDENT, T.NEWLINE, T.EOF]


def test_blank_and_comment_lines_emit_no_layout():
    src = "1\n\n   \n# comment\n2\n"
    assert types(src) == [T.INT, T.NEWLINE, T.INT, T.NEWLINE, T.EOF]


def test_indent_dedent():
    src = "a\n    b\n    c\nd\n"
    assert types(src) == [
        T.IDENT, T.NEWLINE,
        T.INDENT, T.IDENT, T.NEWLINE, T.IDENT, T.NEWLINE,
        T.DEDENT, T.IDENT, T.NEWLINE, T.EOF,
    ]


def test_nested_indent_then_eof_dedents():
    src = "a\n    b\n        c"
    ts = types(src)
    # two INDENTs opened, both closed by EOF (plus the terminating NEWLINE)
    assert ts.count(T.INDENT) == 2
    assert ts.count(T.DEDENT) == 2
    assert ts[-1] is T.EOF


def test_bracket_line_joining_suppresses_layout():
    src = "f(\n    1,\n    2\n)\n"
    # newlines/indentation inside (...) are ignored -> no NEWLINE/INDENT until the ')'
    assert types(src) == [
        T.IDENT, T.LPAREN, T.INT, T.COMMA, T.INT, T.RPAREN, T.NEWLINE, T.EOF,
    ]


def test_eof_terminates_final_statement_without_newline():
    assert types("42")[-2:] == [T.NEWLINE, T.EOF]


def test_tab_in_indentation_is_error():
    with pytest.raises(LexError) as e:
        tokenize("a\n\tb\n", "<t>")
    assert e.value.code == L_TABS_IN_INDENT


def test_bad_dedent_is_error():
    with pytest.raises(LexError) as e:
        tokenize("a\n        b\n    c\n", "<t>")
    assert e.value.code == L_BAD_DEDENT


def test_illegal_char_is_error():
    with pytest.raises(LexError) as e:
        tokenize("a @ b", "<t>")
    assert e.value.code == L_ILLEGAL_CHAR


def test_unterminated_string_is_error():
    with pytest.raises(LexError) as e:
        tokenize('"oops\n', "<t>")
    assert e.value.code == L_UNTERMINATED_STRING


def test_span_offsets_point_at_source():
    toks = tokenize("ab + c", "<t>")
    assert toks[0].type is T.IDENT
    assert (toks[0].span.start, toks[0].span.end) == (0, 2)
    plus = toks[1]
    assert plus.type is T.PLUS
    assert plus.span.start == 3 and plus.span.col == 3


def test_leading_indentation_emits_indent():
    # A top-level line that starts indented emits INDENT (the parser rejects it later).
    assert types("  ab")[0] is T.INDENT


# -- W19: malformed numeric literals are L006, well-placed underscores decode ---

@pytest.mark.parametrize("src", [
    "1_",       # trailing underscore
    "1__0",     # doubled underscore
    "1_.5",     # underscore leading the fraction dot
    "1.5_",     # trailing underscore in a fraction
    "1e3_",     # trailing underscore in an exponent
])
def test_misplaced_underscore_in_number_is_L006(src):
    with pytest.raises(LexError) as e:
        tokenize(src, "<t>")
    assert e.value.code == L_INVALID_NUMBER
    assert e.value.code == "L006"


def test_underscore_separated_ints_decode():
    toks = tokenize("1_000 1_000_000", "<t>")
    assert toks[0].type is T.INT and toks[0].value == 1000
    assert toks[1].type is T.INT and toks[1].value == 1_000_000


def test_leading_underscore_is_an_identifier_not_a_number():
    # `_1` never enters the number path: it lexes as a plain identifier.
    assert types("_1") == [T.IDENT, T.NEWLINE, T.EOF]


def test_digit_dot_underscore_is_field_access_not_L006():
    # DECISIONS.md D12: `1._5` is INT DOT IDENT (rejected later by the checker),
    # not a malformed number — L006 covers digit-adjacent underscores only.
    assert types("1._5") == [T.INT, T.DOT, T.IDENT, T.NEWLINE, T.EOF]
