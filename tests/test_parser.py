"""Parser unit tests (DESIGN.md §8.1): pretty() s-expressions + parse errors."""

from __future__ import annotations

import pytest

from weft.lexer import tokenize
from weft.parser import parse
from weft.errors import ParseError, P_UNEXPECTED_TOKEN, P_UNEXPECTED_EOF, P_TOO_DEEP


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
        "(module (let g (if-expr ((name a) (block (expr-stmt (int 1)))) "
        "(else (block (expr-stmt (int 2)))))))")


def test_inline_if_expression_elif_chain():
    assert pretty("let g = if a: 1 elif b: 2 else: 3\n") == (
        "(module (let g (if-expr "
        "((name a) (block (expr-stmt (int 1)))) "
        "((name b) (block (expr-stmt (int 2)))) "
        "(else (block (expr-stmt (int 3)))))))")


# -- W9: unit literal () -------------------------------------------------------

def test_unit_literal():
    assert pretty("()\n") == "(module (expr-stmt (unit)))"
    assert pretty("let u = ()\n") == "(module (let u (unit)))"
    assert pretty("print(())\n") == "(module (expr-stmt (call (name print) (unit))))"


# -- W10: statement-position + block-form if-expressions ------------------------

def test_statement_position_inline_if_expression():
    # §2.8's own shape: `if c: expr else: expr` as a statement (e.g. a fn body tail)
    assert pretty("if a: f() else: g()\n") == (
        "(module (expr-stmt (if-expr ((name a) (block (expr-stmt (call (name f))))) "
        "(else (block (expr-stmt (call (name g))))))))")


def test_statement_position_if_expr_elif_chain():
    assert pretty("if a: 1 elif b: 2 else: 3\n") == (
        "(module (expr-stmt (if-expr "
        "((name a) (block (expr-stmt (int 1)))) "
        "((name b) (block (expr-stmt (int 2)))) "
        "(else (block (expr-stmt (int 3)))))))")


def test_statement_position_if_expr_in_fn_tail():
    src = "fn f(x: Int) -> Int:\n    if x > 0: 1 else: 2\n"
    assert pretty(src) == (
        "(module (fn f (params (x (type Int))) (ret (type Int)) "
        "(block (expr-stmt (if-expr ((binop > (name x) (int 0)) "
        "(block (expr-stmt (int 1)))) (else (block (expr-stmt (int 2)))))))))")


def test_block_form_if_expression():
    src = "let y = if x > 0:\n    let z = x * 2\n    z + 1\nelse:\n    0\n"
    assert pretty(src) == (
        "(module (let y (if-expr ((binop > (name x) (int 0)) "
        "(block (let z (binop * (name x) (int 2))) (expr-stmt (binop + (name z) (int 1))))) "
        "(else (block (expr-stmt (int 0)))))))")


def test_block_form_if_expression_elif():
    src = "let y = if a:\n    1\nelif b:\n    2\nelse:\n    3\n"
    assert pretty(src) == (
        "(module (let y (if-expr "
        "((name a) (block (expr-stmt (int 1)))) "
        "((name b) (block (expr-stmt (int 2)))) "
        "(else (block (expr-stmt (int 3)))))))")


def test_if_statement_forms_stay_statements():
    # inline branch + NEWLINE: still an if-STATEMENT (may omit else)
    assert pretty("if a: f()\n") == (
        "(module (if ((name a) (block (expr-stmt (call (name f)))))))")
    # inline branch, else on the NEXT line: statement form, not an if-expression
    assert pretty("if a: f()\nelse: g()\n") == (
        "(module (if ((name a) (block (expr-stmt (call (name f))))) "
        "(else (block (expr-stmt (call (name g)))))))")
    # statement-only inline branch heads never commit to the expression form
    assert pretty("loop:\n    if a: break\n") == (
        "(module (loop (block (if ((name a) (block (break)))))))")
    assert pretty("if a: x = 1\n") == (
        "(module (if ((name a) (block (assign x (int 1))))))")


def test_logical_and_not_precedence():
    # `a and not b or c` parses as `(a and (not b)) or c`
    assert pretty("a and not b or c\n") == (
        "(module (expr-stmt (logical or (logical and (name a) (unary not (name b))) (name c))))")


def test_comparison_is_non_associative():
    with pytest.raises(ParseError):
        parse(tokenize("a < b < c\n", "<t>"))


# -- W4: nesting-depth guard (P_TOO_DEEP) -------------------------------------

def test_deep_paren_nesting_raises_p_too_deep():
    src = "(" * 200 + "1" + ")" * 200
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP


def test_deep_not_nesting_raises_p_too_deep():
    src = "not " * 400 + "true"
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP


def test_deep_bracket_nesting_raises_p_too_deep():
    src = "[" * 100
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP


def test_deep_unary_minus_nesting_raises_p_too_deep():
    src = "-" * 400 + "1"
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP


def test_deep_type_nesting_raises_p_too_deep():
    src = "let x: " + "List[" * 200 + "Int" + "]" * 200 + " = y\n"
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP


def test_moderate_nesting_still_parses():
    # Well below the cap: legitimate nesting must not be rejected.
    src = "(" * 20 + "1" + ")" * 20 + "\n"
    parse(tokenize(src, "<t>"))  # must not raise


def test_long_flat_operator_chain_raises_p_too_deep():
    # A flat chain parses iteratively (no nesting), so it bypasses _MAX_DEPTH; the
    # per-chain operand cap catches it instead. 10_001 operands = 10_000 '+' operators.
    src = "let x = " + "1+" * 10_000 + "1\n"
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    assert e.value.code == P_TOO_DEEP
    assert "too long" in e.value.message


def test_long_flat_chain_at_cap_still_parses():
    # 10_000 operands (9_999 operators) is exactly at the cap and must parse.
    src = "let x = " + "1+" * 9_999 + "1\n"
    parse(tokenize(src, "<t>"))  # must not raise


def test_unexpected_indent_message_is_grammatical():
    # Fix 4: layout name for INDENT is "indentation", not "an indented block".
    with pytest.raises(ParseError) as e:
        parse(tokenize("print(1)\n    print(2)\n", "<t>"))
    assert e.value.code == P_UNEXPECTED_TOKEN
    assert "unexpected indentation" in e.value.message
    assert "an indented block" not in e.value.message


def test_empty_parens_type_is_parse_error_with_unit_hint():
    # Fix 6: `()` in type position is not a type; only `() -> R` is.
    with pytest.raises(ParseError) as e:
        parse(tokenize("let u: () = ()\n", "<t>"))
    assert e.value.code == P_UNEXPECTED_TOKEN
    assert "Unit" in e.value.message


def test_zero_arg_fn_type_still_parses():
    parse(tokenize("let f: () -> Int = g\n", "<t>"))  # must not raise


def test_parenthesized_single_type_still_parses():
    parse(tokenize("let x: (Int) = 5\n", "<t>"))       # grouping, unchanged
