"""VM unit tests (DESIGN.md §8.1): hand-assembled Code objects on a StringIO-backed VM."""

from __future__ import annotations

import io

import pytest

from weft.bytecode import Op, Instr, Code
from weft.errors import Span
from weft.vm import VM, VMError


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
