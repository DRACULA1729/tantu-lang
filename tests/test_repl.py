"""REPL unit tests (DESIGN.md §6.4, §8.1): driven with StringIO streams."""

from __future__ import annotations

import io

from weft.repl import repl


def drive(text: str) -> tuple[str, int]:
    out = io.StringIO()
    rc = repl(io.StringIO(text), out)
    return out.getvalue(), rc


def test_bare_expression_is_echoed():
    out, rc = drive("1 + 2 * 3\n")
    assert "7\n" in out
    assert rc == 0


def test_print_is_not_double_echoed():
    out, _ = drive('print("hi")\n')
    assert out.count("hi") == 1


def test_error_does_not_end_the_session():
    out, rc = drive("x\n42\n")
    assert "E0111" in out   # the unknown-name error rendered inline
    assert "42\n" in out    # and the session kept going
    assert rc == 0


def test_bindings_persist_across_inputs():
    out, rc = drive("let x = 10\nx + 5\nvar y = 1\ny = y + x\ny\n")
    assert "15\n" in out    # x persisted into the second input
    assert "11\n" in out    # y = 1 + 10, persisted var mutation
    assert rc == 0


def test_repl_allows_redefinition():
    out, rc = drive("let x = 1\nx\nlet x = 99\nx\n")
    assert "1\n" in out and "99\n" in out   # re-let redefines at the prompt
    assert rc == 0


def test_panic_does_not_end_the_session():
    out, rc = drive("10 / 0\n99\n")
    assert "R001" in out
    assert "99\n" in out
    assert rc == 0


def test_eof_exits_zero():
    _, rc = drive("")
    assert rc == 0
