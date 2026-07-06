"""Checker unit tests (DESIGN.md §8.1): M2 resolution, inference, and semantic errors."""

from __future__ import annotations

import pytest

from weft.lexer import tokenize
from weft.parser import parse
from weft.checker import check
from weft.resolve import Global
from weft.wtypes import INT, BOOL, STR
from weft.errors import (
    CheckErrors, E_UNKNOWN_NAME, E_ASSIGN_IMMUTABLE, E_NON_BOOL_COND,
    E_TYPE_MISMATCH, E_LOOP_CONTROL, E_DUPLICATE, E_UNKNOWN_TYPE,
)


def check_src(src: str):
    return check(parse(tokenize(src, "<t>")))


def errors_of(src: str) -> list:
    with pytest.raises(CheckErrors) as e:
        check_src(src)
    return e.value.errors


def test_binding_resolves_to_global_slot():
    m = check_src("let x = 10\nlet y = 20\n")
    assert isinstance(m.decls[0].res, Global) and m.decls[0].res.index == 0
    assert isinstance(m.decls[1].res, Global) and m.decls[1].res.index == 1
    assert m.n_globals == 2


def test_types_inferred_and_propagated():
    m = check_src('let x = 1 + 2\nlet ok = x < 5\nlet s = "hi"\n')
    assert m.decls[0].value.ty is INT
    assert m.decls[1].value.ty is BOOL
    assert m.decls[2].value.ty is STR


def test_annotation_checked_against_value():
    check_src("let x: Int = 3\n")                # ok
    assert errors_of("let x: Int = true\n")[0].code == E_TYPE_MISMATCH


def test_unknown_type_annotation():
    assert errors_of("let x: Blah = 1\n")[0].code == E_UNKNOWN_TYPE


def test_assign_to_let_is_error():
    assert errors_of("let x = 1\nx = 2\n")[0].code == E_ASSIGN_IMMUTABLE


def test_assign_to_var_is_ok():
    check_src("var x = 1\nx = 2\n")


def test_unknown_name():
    assert errors_of("let y = z\n")[0].code == E_UNKNOWN_NAME


def test_non_bool_condition():
    assert errors_of("if 1:\n    print(1)\n")[0].code == E_NON_BOOL_COND
    assert errors_of("while 2:\n    print(1)\n")[0].code == E_NON_BOOL_COND


def test_arithmetic_type_agreement():
    assert errors_of("let z = 1 + 2.0\n")[0].code == E_TYPE_MISMATCH   # no Int/Float mixing


def test_break_continue_outside_loop():
    assert errors_of("break\n")[0].code == E_LOOP_CONTROL
    assert errors_of("continue\n")[0].code == E_LOOP_CONTROL


def test_break_inside_loop_ok():
    check_src("while true:\n    break\n")
    check_src("loop:\n    continue\n")


def test_duplicate_binding_in_same_scope():
    assert errors_of("let x = 1\nlet x = 2\n")[0].code == E_DUPLICATE


def test_shadowing_in_inner_block_is_ok():
    check_src("let x = 1\nif true:\n    let x = 2\n    print(x)\n")


def test_if_expr_branch_agreement():
    check_src("let g = if true: 1 else: 2\n")
    assert errors_of('let g = if true: 1 else: "x"\n')[0].code == E_TYPE_MISMATCH


def test_errors_are_collected_not_first_only():
    assert len(errors_of("let a = 1 + true\nlet b = c + 2\n")) == 2


def test_error_does_not_cascade():
    # `c` is unknown (1 error); the surrounding comparison must not add a spurious second one
    assert len(errors_of("if c > 0:\n    print(1)\n")) == 1


# --- M3 -------------------------------------------------------------------

from weft.wtypes import FnT, UNIT  # noqa: E402


def test_fn_hoisted_and_main_recorded():
    m = check_src("fn main() -> Unit:\n    print(1)\n")
    assert isinstance(m.decls[0].res, Global)
    assert m.main_slot == m.decls[0].res.index


def test_forward_reference_between_fns():
    check_src("fn a() -> Int:\n    b()\nfn b() -> Int:\n    1\n")   # a calls b, defined later


def test_fn_signature_type():
    m = check_src("fn add(a: Int, b: Int) -> Int:\n    a + b\n")
    assert m.decls[0].fn_ty == FnT((INT, INT), INT)


def test_closure_captures_a_let():
    m = check_src("fn outer(n: Int) -> (Int) -> Int:\n    fn(x: Int) -> Int: x + n\n")
    lam = m.decls[0].body[0].expr          # the inner lambda captures outer's `n`
    assert len(lam.captures) == 1
    assert m.decls[0].captures == []       # outer itself captures nothing


def test_capture_of_var_is_error():
    errs = errors_of("fn main() -> Unit:\n    var c = 0\n    let f = fn() -> Int: c\n    print(f())\n")
    assert errs[0].code == "E0103"


def test_module_var_from_function_is_error():
    assert errors_of("var g = 0\nfn f() -> Int:\n    g\n")[0].code == "E0103"


def test_module_let_readable_from_function():
    check_src("let k = 10\nfn f(x: Int) -> Int:\n    x + k\n")   # immutable global: fine


def test_missing_return_path():
    assert errors_of("fn f(n: Int) -> Int:\n    if n < 0:\n        return 0\n")[0].code == "E0108"


def test_return_type_checked():
    assert errors_of('fn f() -> Int:\n    "hi"\n')[0].code == E_TYPE_MISMATCH


def test_arity_mismatch():
    assert errors_of("fn f(a: Int) -> Int:\n    a\nlet x = f(1, 2)\n")[0].code == "E0118"


def test_return_outside_function():
    assert errors_of("return 1\n")[0].code == "E0116"


def test_calling_a_non_function():
    assert errors_of("let x = 5\nlet y = x(1)\n")[0].code == "E0117"


# --- M4 -------------------------------------------------------------------

from weft.wtypes import EnumT  # noqa: E402


def test_enum_and_variant_construction_type():
    m = check_src("enum Color:\n    Red\n    Custom(Int, Int, Int)\nlet c = Custom(1, 2, 3)\n")
    assert m.decls[1].value.ty == EnumT("Color", ())


def test_option_construction_infers_type_arg():
    m = check_src('let x = Some(3)\n')
    assert m.decls[0].value.ty == EnumT("Option", (INT,))


def test_none_needs_expected_type():
    check_src("let x: Option[Int] = None\n")                 # ok — expected pins T
    assert errors_of("let x = None\n")[0].code == "E0119"    # underdetermined


def test_result_err_infers_from_args_and_expected():
    check_src('fn f() -> Result[Int, Str]:\n    Err("bad")\n')   # E from arg, T from expected


def test_match_exhaustive_ok():
    check_src("enum C:\n    A\n    B\nfn f(c: C) -> Int:\n    match c:\n        A => 1\n        B => 2\n")


def test_match_non_exhaustive():
    errs = errors_of("enum C:\n    A\n    B\nfn f(c: C) -> Int:\n    match c:\n        A => 1\n")
    assert errs[0].code == "E0105" and "B" in errs[0].message


def test_match_unreachable_arm():
    assert errors_of("fn f(n: Int) -> Int:\n    match n:\n        _ => 0\n        1 => 1\n")[0].code == "E0120"


def test_int_match_needs_wildcard():
    assert errors_of("fn f(n: Int) -> Int:\n    match n:\n        0 => 0\n        1 => 1\n")[0].code == "E0105"


def test_variant_pattern_binds_payload_type():
    # `Some(x)` on Option[Int] binds x: Int, so `x + 1` type-checks
    check_src("fn f(o: Option[Int]) -> Int:\n    match o:\n        Some(x) => x + 1\n        None => 0\n")


def test_wrong_variant_in_pattern():
    src = "enum A:\n    X\nenum B:\n    Y\nfn f(a: A) -> Int:\n    match a:\n        Y => 1\n        _ => 0\n"
    assert errors_of(src)[0].code == "E0121"


def test_constructor_not_first_class():
    assert errors_of("let f = Some\n")[0].code == "E0123"


def test_duplicate_variant_name():
    assert errors_of("enum A:\n    Dup\nenum B:\n    Dup\n")[0].code == "E0124"


# --- M5 -------------------------------------------------------------------

from weft.wtypes import ListT, TupleT, StructT  # noqa: E402


def test_list_and_index_types():
    m = check_src("let xs = [1, 2, 3]\nlet a = xs[0]\n")
    assert m.decls[0].value.ty == ListT(INT)
    assert m.decls[1].value.ty is INT


def test_heterogeneous_list_is_error():
    assert errors_of('let xs = [1, "two"]\n')[0].code == E_TYPE_MISMATCH


def test_empty_list_needs_annotation():
    check_src("let xs: List[Int] = []\n")                    # ok — annotated
    assert errors_of("let xs = []\n")[0].code == "E0107"     # bare → cannot infer


def test_tuple_and_destructuring():
    m = check_src('let t = (1, "a")\nlet (n, w) = t\n')
    assert m.decls[0].value.ty == TupleT((INT, STR))


def test_struct_construction_and_field():
    m = check_src("struct P:\n    x: Int\n    y: Int\nlet p = P(1, 2)\nlet a = p.x\n")
    assert m.decls[1].value.ty == StructT("P")
    assert m.decls[2].value.ty is INT


def test_unknown_field():
    assert errors_of("struct P:\n    x: Int\nlet p = P(1)\nlet a = p.z\n")[0].code == E_UNKNOWN_NAME


def test_builtin_overload_resolution():
    m = check_src('let a = len([1, 2, 3])\nlet b = len("hello")\n')
    assert m.decls[0].value.ty is INT and m.decls[1].value.ty is INT


def test_get_returns_option():
    m = check_src("let o = get([1, 2], 0)\n")
    assert m.decls[0].value.ty == EnumT("Option", (INT,))


def test_builtin_not_first_class():
    assert errors_of("let f = len\n")[0].code == "E0109"


def test_for_over_non_list_is_error():
    assert errors_of("fn main() -> Unit:\n    for x in 5:\n        print(x)\n")[0].code == E_TYPE_MISMATCH


def test_for_var_type_flows():
    check_src("fn main() -> Unit:\n    for x in [1, 2, 3]:\n        print(x + 1)\n")   # x: Int


# --- M6 -------------------------------------------------------------------

def test_question_on_result_yields_payload():
    check_src("fn g() -> Result[Int, Str]:\n    Ok(1)\n"
              "fn f() -> Result[Int, Str]:\n    let n = g()?\n    Ok(n + 1)\n")


def test_question_on_option_yields_payload():
    check_src("fn f(s: Str) -> Option[Int]:\n    let n = parse_int(s)?\n    Some(n + 1)\n")


def test_question_result_in_option_fn_is_error():
    src = ("fn g() -> Result[Int, Str]:\n    Ok(1)\n"
           "fn f() -> Option[Int]:\n    let n = g()?\n    Some(n)\n")
    assert errors_of(src)[0].code == "E0106"


def test_question_on_plain_value_is_error():
    assert errors_of("fn f() -> Result[Int, Str]:\n    let n = 5?\n    Ok(n)\n")[0].code == "E0106"


def test_bidirectional_if_infers_variant():
    # a block-form if tail infers Ok/Err because the return type flows into the branches
    check_src('fn f(n: Int) -> Result[Int, Str]:\n'
              '    if n > 0:\n        Ok(n)\n    else:\n        Err("neg")\n')


def test_bidirectional_match_arm_infers_variant():
    check_src('fn f(o: Option[Int]) -> Result[Int, Str]:\n'
              '    match o:\n        Some(n) => Ok(n)\n        None => Err("none")\n')


# -- W2: '?' / 'return' inside an unannotated inline lambda -------------------

def test_question_in_unannotated_inline_lambda_is_error():
    # soundness repro: previously checked clean and let the Err escape as the payload
    src = ('fn get() -> Result[Int, Str]:\n    Err("boom")\n'
           'fn outer() -> Result[Int, Str]:\n'
           '    let f = fn(): get()?\n    let x = f()\n    Ok(x)\n')
    errs = errors_of(src)
    assert errs[0].code == "E0106"
    assert "return-type annotation" in errs[0].message


def test_question_in_annotated_inline_lambda_ok():
    check_src('fn get() -> Result[Int, Str]:\n    Err("boom")\n'
              'fn outer() -> Result[Int, Str]:\n'
              '    let f = fn() -> Result[Int, Str]: Ok(get()?)\n    f()\n')


def test_return_in_match_inside_unannotated_inline_lambda_is_error():
    src = ('let f = fn(b: Bool): match b:\n'
           '    true => return 1\n'
           '    false => 2\n')
    errs = errors_of(src)
    assert errs[0].code == "E0106"
    assert "'return' inside a lambda" in errs[0].message


def test_module_level_inline_lambda_question_message():
    # previously misreported as "'?' is only valid inside a function"
    src = ('fn get() -> Result[Int, Str]:\n    Err("boom")\n'
           'let f = fn(): get()?\n')
    errs = errors_of(src)
    assert errs[0].code == "E0106"
    assert errs[0].message == \
        "'?' inside a lambda without a return-type annotation; add '-> T'"
