"""CLI exception-boundary tests (W1): no input may ever produce a Python traceback.

Subprocess tests against `python -m weft` covering invalid-UTF-8 source files and
binary stdin. Exit-code contract (DESIGN.md §6.2): 2 for compile-time/usage/internal
errors, with a clean one-line diagnostic on stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys


def run_weft(args: list[str], stdin: bytes = b"") -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "weft", *args],
        input=stdin, capture_output=True, timeout=60, env=env,
    )


def _decoded(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).decode("utf-8", "replace")


def test_invalid_utf8_file_check_exits_2_cleanly(tmp_path):
    # Latin-1 "café" — the 0xe9 byte is not valid UTF-8.
    bad = tmp_path / "bad.weft"
    bad.write_bytes(b'print("caf\xe9")\n')
    result = run_weft(["check", str(bad)])
    out = _decoded(result)
    assert result.returncode == 2, out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out


def test_invalid_utf8_file_run_exits_2_cleanly(tmp_path):
    bad = tmp_path / "bad.weft"
    bad.write_bytes(b"\xff\xfe let x = 1\n")
    result = run_weft(["run", str(bad)])
    out = _decoded(result)
    assert result.returncode == 2, out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out


def test_binary_stdin_run_exits_2_cleanly():
    result = run_weft(["run", "-"], stdin=b"\xff\xfe\x00\x01binary\x9c")
    out = _decoded(result)
    assert result.returncode == 2, out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out


def test_binary_stdin_check_exits_2_cleanly():
    result = run_weft(["check", "-"], stdin=b"\x80\x81\x82")
    out = _decoded(result)
    assert result.returncode == 2, out
    assert "Traceback" not in out
    assert "not valid UTF-8" in out


def test_valid_stdin_still_runs():
    """The stdin path still works for well-formed UTF-8 source (no regression)."""
    result = run_weft(["run", "-"], stdin=b'print("hi")\n')
    out = _decoded(result)
    assert result.returncode == 0, out
    assert "hi" in out


def test_deeply_nested_input_check_exits_2_cleanly():
    """W4: deep nesting is a diagnosed parse error (P003), never a Python crash."""
    src = ("not " * 400 + "true\n").encode("utf-8")
    result = run_weft(["check", "-"], stdin=src)
    out = _decoded(result)
    assert result.returncode == 2, out
    assert "P003" in out
    assert "Traceback" not in out
