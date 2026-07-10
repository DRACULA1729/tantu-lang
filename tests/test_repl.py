"""REPL unit tests (DESIGN.md §6.4, §8.1): driven with StringIO streams."""

from __future__ import annotations

import io
import os
import subprocess
import sys

import weft.repl
from weft.repl import repl


def drive(text: str) -> tuple[str, int]:
    out = io.StringIO()
    rc = repl(io.StringIO(text), out)
    return out.getvalue(), rc


def assert_no_traceback(out: str) -> None:
    """Shared W1 invariant: no raw Python traceback may ever reach the user."""
    assert "Traceback" not in out, f"raw Python traceback leaked:\n{out}"


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


def test_internal_error_does_not_end_the_session(monkeypatch):
    """An unexpected (non-Weft) exception during evaluation degrades to a one-line
    internal-error message and the session keeps going (W1 catch-all boundary)."""
    orig = weft.repl._Session.run_input

    def exploding_run_input(self, src):
        if "boom" in src:
            raise RuntimeError("synthetic evaluator bug")
        return orig(self, src)

    monkeypatch.setattr(weft.repl._Session, "run_input", exploding_run_input)
    out, rc = drive("boom\n1 + 1\n")
    assert "internal error" in out
    assert "synthetic evaluator bug" in out
    assert "2\n" in out          # a following valid line still evaluates
    assert rc == 0
    assert_no_traceback(out)


def test_keyboard_interrupt_does_not_end_the_session(monkeypatch):
    """Ctrl-C during evaluation prints KeyboardInterrupt and returns to the prompt."""
    orig = weft.repl._Session.run_input

    def interrupted_run_input(self, src):
        if "boom" in src:
            raise KeyboardInterrupt
        return orig(self, src)

    monkeypatch.setattr(weft.repl._Session, "run_input", interrupted_run_input)
    out, rc = drive("boom\n40 + 2\n")
    assert "KeyboardInterrupt" in out
    assert "42\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_deep_nesting_does_not_end_the_session():
    """W4: a too-deep expression is diagnosed (P003) and the REPL keeps going."""
    out, rc = drive("not " * 400 + "true\n40 + 2\n")
    assert "P003" in out
    assert "42\n" in out
    assert rc == 0
    assert_no_traceback(out)


# --- multi-line continuation (DESIGN §6.4: first-line decision, blank line submits) ---


def test_multiline_fn_body_then_call():
    """A multi-statement fn body entered across lines (blank line submits), then called."""
    out, rc = drive(
        "fn add3(a: Int, b: Int, c: Int) -> Int:\n"
        "    let s = a + b\n"
        "    return s + c\n"
        "\n"                     # blank line submits the block
        "add3(1, 2, 3)\n"
    )
    assert "6\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_crlf_multiline_fn_body():
    """A CRLF-line-ended script piped to the REPL (each line normalized to LF) works the
    same as an LF one — the continuation and lexer never see a stray '\\r'."""
    out, rc = drive(
        "fn add3(a: Int, b: Int, c: Int) -> Int:\r\n"
        "    let s = a + b\r\n"
        "    return s + c\r\n"
        "\r\n"                    # blank (CRLF) line submits the block
        "add3(1, 2, 3)\r\n"
    )
    assert "6\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_multiline_match_expression():
    """A multi-arm match entered across lines evaluates."""
    out, rc = drive(
        "let o = Some(41)\n"
        "match o:\n"
        "    Some(n) => n + 1\n"
        "    None => 0\n"
        "\n"
        "9\n"                    # session still alive after the block
    )
    assert "42\n" in out
    assert "9\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_multiline_if_else_block():
    out, rc = drive(
        "if 1 < 2:\n"
        '    print("yes")\n'
        "else:\n"
        '    print("no")\n'
        "\n"
    )
    assert "yes\n" in out
    assert "no\n" not in out
    assert rc == 0
    assert_no_traceback(out)


def test_comment_with_open_paren_does_not_wedge():
    """FE-6: a comment line containing '(' must not enter continuation or wedge."""
    out, rc = drive("# note (\n1 + 1\n")
    assert "2\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_comment_ending_in_colon_does_not_continue():
    """FE-6: a trailing comment ending in ':' is not a block header."""
    out, rc = drive("# heads up:\n40 + 2\n")
    assert "42\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_trailing_comment_colon_on_code_line_does_not_continue():
    """A code line whose comment ends in ':' submits immediately (comment is stripped)."""
    out, rc = drive("1 + 2  # sum:\n")
    assert "3\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_hash_inside_string_is_not_a_comment():
    """String-aware stripping: '#' inside a string literal is not a comment."""
    out, rc = drive('print("a # b")\n')
    assert "a # b\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_buffer_ending_in_colon_terminates_on_blank_line():
    """A buffer whose LAST line ends in ':' still submits on the blank line — the
    continuation decision is made once, on the first line (no loop-to-EOF)."""
    out, rc = drive(
        "if true:\n"
        "    if false:\n"       # buffer now ends in ':'
        "\n"                     # blank line must still submit (then a parse/check error is fine)
        "40 + 2\n"               # and the session keeps going
    )
    assert "42\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_bracket_continuation_across_lines():
    """A '[' opened on line 1 and closed later; blank line submits the buffer."""
    out, rc = drive(
        "let xs = [1,\n"
        "2,\n"
        "3]\n"
        "\n"
        "len(xs)\n"
    )
    assert "3\n" in out
    assert rc == 0
    assert_no_traceback(out)


def test_eof_mid_continuation_submits_and_exits():
    """EOF while in continuation mode submits the buffer and exits 0 (never hangs)."""
    out, rc = drive(
        "fn one() -> Int:\n"
        "    return 1\n"        # EOF here: block submits, session then sees EOF and exits
    )
    assert rc == 0
    assert_no_traceback(out)


def test_repl_broken_stdout_pipe_exits_not_hang():
    """F2 (anti-hang): with the REPL's stdout read end closed, the prompt flush raises
    BrokenPipeError. The loop must EXIT (rc 141), not busy-loop forever writing internal-error
    lines. A real subprocess is used so a regression genuinely hangs (caught by the timeout)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "weft"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        proc.stdout.close()                 # the REPL's output consumer went away
        proc.stdin.write(b"1 + 1\n")        # feed one input, then EOF
        proc.stdin.flush()
        proc.stdin.close()
        rc = proc.wait(timeout=10)          # MUST terminate; a hang fails here
        err = proc.stderr.read().decode("utf-8", "replace")
        assert rc == 141, err
        for bad in ("Traceback", "Exception ignored", "internal error"):
            assert bad not in err, err
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
