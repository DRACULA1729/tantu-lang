"""M9b user-generics tests: parse shape, type inference, opaque-T rules, and erasure.

Generics are ERASED (Fable ruling): checked polymorphically, compiled to one Code. The
end-to-end behavior lives in golden/run/generic_*.tn + golden/modules/generic_lib; these
unit tests lock the per-stage contracts and the diagnostic surface.
"""

from __future__ import annotations

import pytest

from tantu.lexer import tokenize
from tantu.parser import parse
from tantu.checker import check
from tantu.ttypes import INT, STR
from tantu.cli import front
from tantu.errors import (
    CheckErrors, ParseError,
    E_GENERIC_NOT_VALUE, E_CANNOT_INFER, E_TYPE_MISMATCH, E_NOT_COMPARABLE,
    E_NON_BOOL_COND, E_UNKNOWN_TYPE, E_DUPLICATE, E_UNSUPPORTED,
)


# --------------------------------------------------------------------- parsing

def _pretty(src: str) -> str:
    return parse(tokenize(src, "<t>")).pretty()


def test_generic_fn_pretty():
    assert "(fn map (tparams T U)" in _pretty("fn map[T, U](xs: List[T]) -> List[T]:\n    xs\n")


def test_generic_enum_pretty():
    assert "(enum Tree (tparams T)" in _pretty("enum Tree[T]:\n    Leaf\n    Node(T)\n")


def test_generic_struct_pretty():
    assert "(struct Pair (tparams A B)" in _pretty("struct Pair[A, B]:\n    fst: A\n    snd: B\n")


def test_monomorphic_pretty_unchanged():
    # no [T] => no (tparams ...) segment (existing goldens must not shift)
    assert _pretty("fn f(x: Int) -> Int:\n    x\n") == \
        "(module (fn f (params (x (type Int))) (ret (type Int)) (block (expr-stmt (name x)))))"


def test_empty_type_param_list_is_parse_error():
    with pytest.raises(ParseError):
        parse(tokenize("fn f[]() -> Int:\n    1\n", "<t>"))


# --------------------------------------------------------------------- checking (positive)

def _check(src: str):
    return front(src, "<t>")


def test_identity_instantiates_at_each_call_site():
    m = _check("fn id[T](x: T) -> T:\n    x\n"
               "fn main() -> Unit:\n    print(id(42))\n    print(id(\"hi\"))\n")
    # the two calls to id resolve to Int and Str respectively
    main = [d for d in m.decls if getattr(d, "name", None) == "main"][0]
    call_int = main.body[0].expr.args[0]      # id(42)
    call_str = main.body[1].expr.args[0]      # id("hi")
    assert call_int.ty == INT and call_str.ty == STR


def test_expected_type_pins_underdetermined_param():
    _check("fn none_of[T]() -> Option[T]:\n    None\n"
           "fn main() -> Unit:\n    let z: Option[Int] = none_of()\n    print(\"x\")\n")


def test_generic_struct_field_substitutes():
    m = _check("struct Pair[A, B]:\n    first: A\n    second: B\n"
               "fn main() -> Unit:\n    let p: Pair[Int, Str] = Pair(1, \"a\")\n"
               "    print(p.second)\n")
    main = [d for d in m.decls if getattr(d, "name", None) == "main"][0]
    field = main.body[1].expr.args[0]         # p.second
    assert field.ty == STR


def test_recursive_generic_enum_and_polymorphic_recursion():
    _check("enum Tree[T]:\n    Leaf\n    Node(Tree[T], T, Tree[T])\n"
           "fn size[T](t: Tree[T]) -> Int:\n    match t:\n        Leaf => 0\n"
           "        Node(l, _, r) => 1 + size(l) + size(r)\n"
           "fn main() -> Unit:\n    let t: Tree[Int] = Node(Leaf, 1, Leaf)\n    print(size(t))\n")


def test_question_operator_in_generic_fn():
    _check("fn seq[T, E](a: Result[T, E]) -> Result[T, E]:\n    let x = a?\n    Ok(x)\n")


def test_generic_over_channel():
    _check("fn firstval[T](c: Chan[T]) -> Option[T]:\n    recv(c)\n")


def test_generic_calls_generic():
    _check("fn id[T](x: T) -> T:\n    x\n"
           "fn twice[U](x: U) -> U:\n    id(id(x))\n")


def test_lambda_annotated_with_enclosing_param():
    _check("fn apply[T](x: T, f: (T) -> T) -> T:\n    f(x)\n"
           "fn use[T](x: T) -> T:\n    apply(x, fn(y: T) -> T: y)\n")


# --------------------------------------------------------------------- checking (negative)

def _codes(src: str) -> list:
    with pytest.raises(CheckErrors) as e:
        _check(src)
    return [x.code for x in e.value.errors]


_GEN = "fn id[T](x: T) -> T:\n    x\n"


def test_generic_fn_is_not_a_value():
    assert E_GENERIC_NOT_VALUE in _codes(_GEN + "fn main() -> Unit:\n    let g = id\n    print(1)\n")


def test_generic_fn_as_argument_is_not_a_value():
    src = (_GEN + "fn call2(f: (Int) -> Int) -> Int:\n    f(2)\n"
           "fn main() -> Unit:\n    print(call2(id))\n")
    assert E_GENERIC_NOT_VALUE in _codes(src)


def test_underdetermined_call_is_e0119():
    assert E_CANNOT_INFER in _codes(
        "fn none_of[T]() -> Option[T]:\n    None\n"
        "fn main() -> Unit:\n    let z = none_of()\n    print(1)\n")


def test_arithmetic_on_opaque_t():
    assert E_TYPE_MISMATCH in _codes("fn bad[T](x: T) -> T:\n    x + x\n")


def test_equality_on_opaque_t():
    assert E_NOT_COMPARABLE in _codes("fn bad[T](x: T, y: T) -> Bool:\n    x == y\n")


def test_condition_on_opaque_t():
    assert E_NON_BOOL_COND in _codes(
        "fn bad[T](x: T) -> Int:\n    if x:\n        1\n    else:\n        2\n")


def test_unbound_type_param_in_signature():
    assert E_UNKNOWN_TYPE in _codes("fn bad(x: Int) -> T:\n    x\n")


def test_duplicate_type_param():
    assert _codes("fn bad[T, T](x: T) -> T:\n    x\n") == [E_DUPLICATE]


def test_type_param_shadows_existing_type():
    assert E_DUPLICATE in _codes("fn bad[Int](x: Int) -> Int:\n    x\n")


# ------------------------------------------ underdetermined-arg error does not cascade (Fix 1)

def _errs(src: str) -> list:
    with pytest.raises(CheckErrors) as e:
        _check(src)
    return e.value.errors


_PAIR = "fn pair[T](a: T, b: T) -> T:\n    a\n"


def test_uninferable_arg_reports_only_e0119_not_cascade():
    # None cannot pin T, so E0119 is the root; the valid Some(1) arg and the annotation
    # must NOT produce follow-on E0112s, and the ERROR sentinel must never leak into a message.
    errs = _errs(_PAIR + "let o: Option[Int] = pair(None, Some(1))\n")
    assert [x.code for x in errs] == [E_CANNOT_INFER]
    assert all("<error>" not in x.message for x in errs)


def test_uninferable_arg_no_annotation_single_e0119():
    errs = _errs(_GEN + "let z = id(None)\n")
    assert [x.code for x in errs] == [E_CANNOT_INFER]
    assert all("<error>" not in x.message for x in errs)


def test_uninferable_arg_with_annotation_single_e0119():
    errs = _errs(_GEN + "let z: Option[Int] = id(None)\n")
    assert [x.code for x in errs] == [E_CANNOT_INFER]
    assert all("<error>" not in x.message for x in errs)


def test_genuinely_wrong_generic_call_still_errors():
    # pair(1, "s"): T pinned to Int by the first arg, so the Str second arg is a real mismatch.
    codes = _codes(_PAIR + 'let o = pair(1, "s")\n')
    assert codes == [E_TYPE_MISMATCH]


def test_type_arg_arity_mismatch():
    assert E_UNKNOWN_TYPE in _codes(
        "enum Tree[T]:\n    Leaf\nfn bad(t: Tree[Int, Str]) -> Int:\n    0\n")


def test_applied_type_param():
    assert E_UNKNOWN_TYPE in _codes("fn bad[T](x: T[Int]) -> Int:\n    0\n")


def test_generic_main_is_rejected():
    assert E_UNSUPPORTED in _codes("fn main[T]() -> Unit:\n    print(1)\n")
