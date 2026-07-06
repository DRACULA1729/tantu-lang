"""Parser unit tests (DESIGN.md §8.1): pretty() s-expressions + parse errors."""

from __future__ import annotations

import pytest

from weft.lexer import tokenize
from weft.parser import parse
from weft.errors import ParseError, P_UNEXPECTED_TOKEN, P_UNEXPECTED_EOF


def pretty(src: str) -> str:
    return parse(tokenize(src, "<t>")).pretty()


def test_precedence_mul_binds_tighter():
    assert pretty("1 + 2 * 3") == "(module (expr-stmt (binop + (int 1) (binop * (int 2) (int 3)))))"


def test_left_associativity():
    assert pretty("1 - 2 - 3") == "(module (expr-stmt (binop - (binop - (int 1) (int 2)) (int 3))))"


def test_parens_override_precedence():
    assert pretty("(1 + 2) * 3") == "(module (expr-stmt (binop * (binop + (int 1) (int 2)) (int 3))))"


def test_unary_negate():
    assert pretty("-5 + 3") == "(module (expr-stmt (binop + (unary - (int 5)) (int 3))))"


def test_call_with_args():
    assert pretty("print(1, 2)") == "(module (expr-stmt (call (name print) (int 1) (int 2))))"


def test_string_and_bool_and_float_literals():
    assert pretty('print(3.5)') == "(module (expr-stmt (call (name print) (float 3.5))))"
    assert pretty('true') == "(module (expr-stmt (bool true)))"


def test_multiple_statements():
    assert pretty("1\n2\n") == "(module (expr-stmt (int 1)) (expr-stmt (int 2)))"


def test_empty_module():
    assert pretty("") == "(module)"


def test_error_dangling_operator():
    with pytest.raises(ParseError) as e:
        parse(tokenize("1 +\n", "<t>"))
    assert e.value.code == P_UNEXPECTED_TOKEN


def test_error_trailing_token():
    with pytest.raises(ParseError) as e:
        parse(tokenize("1 2\n", "<t>"))
    assert e.value.code == P_UNEXPECTED_TOKEN


def test_error_unclosed_paren_hits_eof():
    with pytest.raises(ParseError) as e:
        parse(tokenize("(1 + 2", "<t>"))
    assert e.value.code == P_UNEXPECTED_EOF


# --- M2 -------------------------------------------------------------------

def test_let_var_assign():
    assert pretty("let x: Int = 1\nvar y = 2\ny = 3\n") == (
        "(module (let x (type Int) (int 1)) (var y (int 2)) (assign y (int 3)))")


def test_if_elif_else_statement():
    assert pretty("if a:\n    b\nelif c:\n    d\nelse:\n    e\n") == (
        "(module (if ((name a) (block (expr-stmt (name b)))) "
        "((name c) (block (expr-stmt (name d)))) (else (block (expr-stmt (name e))))))")


def test_inline_block():
    assert pretty("if a: b\n") == "(module (if ((name a) (block (expr-stmt (name b))))))"


def test_while_loop_break_continue():
    assert pretty("loop:\n    break\n") == "(module (loop (block (break))))"
    assert pretty("while a:\n    continue\n") == (
        "(module (while (name a) (block (continue))))")


def test_inline_if_expression():
    assert pretty("let g = if a: 1 else: 2\n") == (
        "(module (let g (if-expr ((name a) (int 1)) (else (int 2)))))")


def test_logical_and_not_precedence():
    # `a and not b or c` parses as `(a and (not b)) or c`
    assert pretty("a and not b or c\n") == (
        "(module (expr-stmt (logical or (logical and (name a) (unary not (name b))) (name c))))")


def test_comparison_is_non_associative():
    with pytest.raises(ParseError):
        parse(tokenize("a < b < c\n", "<t>"))
