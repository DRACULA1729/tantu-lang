"""VM unit tests (DESIGN.md §8.1): hand-assembled Code objects on a StringIO-backed VM."""

from __future__ import annotations

import io

import pytest

from tantu.bytecode import Op, Instr, Code
from tantu.errors import Span
from tantu.vm import VM, VMError


def _span() -> Span:
    return Span("<t>", 0, 0, 1, 0)


def _code(instrs: list[Instr], consts: list) -> Code:
    return Code("<module>", 0, 0, 0, instrs, consts, [_span() for _ in instrs])


def _run(code: Code) -> tuple[str, int]:
    out = io.StringIO()
    rc = VM(stdout=out).run(code, "1 / 0", "<t>")
    return out.getvalue(), rc


def test_arithmetic_and_print():
    code = _code([
        Instr(Op.LOAD_CONST, 0), Instr(Op.LOAD_CONST, 1), Instr(Op.LOAD_CONST, 2),
        Instr(Op.MUL), Instr(Op.ADD), Instr(Op.PRINT), Instr(Op.POP), Instr(Op.HALT),
    ], [1, 2, 3])
    assert _run(code) == ("7\n", 0)


def test_int_floor_division():
    code = _code([Instr(Op.LOAD_CONST, 0), Instr(Op.LOAD_CONST, 1),
                  Instr(Op.DIV), Instr(Op.PRINT), Instr(Op.POP), Instr(Op.HALT)], [17, 5])
    assert _run(code) == ("3\n", 0)


def test_float_true_division():
    code = _code([Instr(Op.LOAD_CONST, 0), Instr(Op.LOAD_CONST, 1),
                  Instr(Op.DIV), Instr(Op.PRINT), Instr(Op.POP), Instr(Op.HALT)], [7.0, 2.0])
    assert _run(code) == ("3.5\n", 0)


def test_negate():
    code = _code([Instr(Op.LOAD_CONST, 0), Instr(Op.NEG),
                  Instr(Op.PRINT), Instr(Op.POP), Instr(Op.HALT)], [5])
    assert _run(code) == ("-5\n", 0)


def test_div_by_zero_panics_exit_1():
    code = _code([Instr(Op.LOAD_CONST, 0), Instr(Op.LOAD_CONST, 1),
                  Instr(Op.DIV), Instr(Op.PRINT), Instr(Op.POP), Instr(Op.HALT)], [1, 0])
    out, rc = _run(code)
    assert rc == 1
    assert "R001" in out and "^" in out  # caret'd panic trace


def test_calling_a_non_callable_raises_vmerror():
    # every opcode is wired now; the internal-error path is CALL on a non-function value
    code = _code([Instr(Op.LOAD_CONST, 0), Instr(Op.CALL, 0), Instr(Op.HALT)], [42])
    with pytest.raises(VMError):
        VM(stdout=io.StringIO()).run(code, "", "<t>")


# -- W5: builtin panic boundary + total builtins (full pipeline via the golden harness) -------

from harness import run_capture
from tantu.values import render


def test_split_empty_separator_returns_chars():
    out, err, code = run_capture('print(split("abc", ""))', "split.tn")
    assert err == ""
    assert (out, code) == ('["a", "b", "c"]\n', 0)


def test_float_overflow_returns_inf():
    # DESIGN §2.3: Float overflow is inf, a legal value — not a host OverflowError.
    out, err, code = run_capture(f"print(float({10 ** 500}))", "big.tn")
    assert err == ""
    assert (out, code) == ("inf\n", 0)


def test_trunc_inf_panics_r009():
    out, err, code = run_capture("print(trunc(1.0e300 * 1.0e300))", "inf.tn")
    assert code == 1
    assert err == ""
    assert "panic [R009]" in out and "trunc" in out
    assert "^" in out                       # caret'd panic trace, not a raw traceback
    assert "Traceback" not in out


def test_trunc_nan_panics_r009():
    out, err, code = run_capture("print(trunc(0.0 * (1.0e300 * 1.0e300)))", "nan.tn")
    assert code == 1
    assert err == ""
    assert "panic [R009]" in out and "nan" in out and "^" in out
    assert "Traceback" not in out


# -- W6: iterative render() — deep values must not blow Python's recursion limit -------------


def test_render_deep_recursive_value():
    src = (
        "enum L:\n"
        "    Nil\n"
        "    Cons(Int, L)\n"
        "\n"
        "fn main() -> Unit:\n"
        "    var xs: L = Nil\n"
        "    var i = 0\n"
        "    while i < 5000:\n"
        "        xs = Cons(i, xs)\n"
        "        i = i + 1\n"
        "    print(xs)\n"
    )
    out, err, code = run_capture(src, "deep.tn")
    assert err == ""
    assert code == 0
    assert out.startswith("Cons(4999, Cons(4998, ")
    assert out.count("Cons(") == 5000
    assert out.endswith("Cons(0, Nil" + ")" * 5000 + "\n")


def test_render_deep_python_value_direct():
    v: object = 1
    for _ in range(5000):
        v = [v]
    assert render(v) == "[" * 5000 + "1" + "]" * 5000


# -- W17: Tantu call-depth cap (R010) + truncated panic trace ---------------------------------

from tantu.cli import compile_source

_RUNAWAY = "fn f(n: Int) -> Int:\n    return f(n + 1)\n\nfn main() -> Unit:\n    print(f(0))\n"


def test_runaway_recursion_panics_r010_with_truncated_trace():
    out, err, code = run_capture(_RUNAWAY, "so.tn")
    assert code == 1
    assert err == ""
    assert "panic [R010]" in out and "call stack overflow (100000 frames)" in out
    assert "more frames" in out                  # the truncation marker
    assert len(out.splitlines()) < 100           # ~20 frames rendered, not 100k
    assert "Traceback" not in out


def test_max_frames_is_a_ctor_param():
    code = compile_source(_RUNAWAY, "so2.tn")
    out = io.StringIO()
    rc = VM(stdout=out, max_frames=50).run(code, _RUNAWAY, "so2.tn")
    assert rc == 1
    assert "call stack overflow (50 frames)" in out.getvalue()


def test_deep_but_legal_recursion_still_succeeds():
    # 50k frames sits well under the 100k default cap — deep-but-legal recursion must work.
    src = ("fn f(n: Int) -> Int:\n    if n == 0:\n        return 0\n    return f(n - 1) + 1\n\n"
           "fn main() -> Unit:\n    print(f(50000))\n")
    out, err, code = run_capture(src, "deep_rec.tn")
    assert (err, code) == ("", 0)
    assert out == "50000\n"


# -- W18: negative channel capacity (R011) ----------------------------------------------------


def test_negative_channel_capacity_panics_r011():
    out, err, code = run_capture(
        "fn main() -> Unit:\n    let c = chan[Int](-5)\n    send(c, 1)\n", "nc.tn")
    assert code == 1
    assert err == ""
    assert "panic [R011]" in out and "channel capacity cannot be negative (-5)" in out
