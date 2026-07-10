"""CLI exception-boundary tests (W1): no input may ever produce a Python traceback.

Subprocess tests against `python -m weft` covering invalid-UTF-8 source files and
binary stdin. Exit-code contract (DESIGN.md §6.2): 2 for compile-time/usage/internal
errors, with a clean one-line diagnostic on stderr.
"""

from __future__ import annotations

import io
import os
import re
import select
import signal
import subprocess
import sys
import time

import pytest

from weft.cli import main


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


# A looping program that first prints a marker, so the test can wait for the loop to be
# genuinely running before signalling — a fixed sleep would flake on a slow CI runner if the
# signal landed during Python startup (before weft's handler exists). Run with `-u` so the
# marker flushes immediately (weft's VM print does not flush, and a PIPE is block-buffered).
_LOOP_PROG = 'fn main():\n    print("running")\n    loop:\n        let x = 1\n\nmain()\n'


def _spawn_looping_weft(prog_path):
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "weft", "run", str(prog_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _wait_for_marker(proc, timeout=10):
    """Block until the child emits its startup marker line, so we know its SIGINT handler is
    installed and the loop is running. Bounded by `timeout` to avoid hanging a stuck test."""
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    assert ready, "child did not emit its startup marker in time"
    line = proc.stdout.readline()
    assert b"running" in line, line


def test_sigint_during_run_exits_130_without_traceback(tmp_path):
    """W20/blocker: Ctrl-C during `weft run` exits 130 with a clean one-line message,
    never a raw Python traceback. Popen is used directly so we deliver SIGINT ourselves
    after the program is genuinely looping forever."""
    prog = tmp_path / "loop.weft"
    prog.write_text(_LOOP_PROG)
    proc = _spawn_looping_weft(prog)
    try:
        _wait_for_marker(proc)          # loop is running and the handler is installed
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        combined = (out + err).decode("utf-8", "replace")
        assert proc.returncode == 130, combined
        assert "Traceback" not in combined
        assert "interrupted" in err.decode("utf-8", "replace")
    finally:
        if proc.poll() is None:         # failure path: don't leave the loop spinning forever
            proc.kill()
            proc.wait()


def test_double_sigint_during_run_exits_130_without_traceback(tmp_path):
    """A second Ctrl-C arriving during teardown must not resurface as a CPython
    `threading._shutdown` traceback: the handler ignores SIGINT after the first hit."""
    prog = tmp_path / "loop.weft"
    prog.write_text(_LOOP_PROG)
    proc = _spawn_looping_weft(prog)
    try:
        _wait_for_marker(proc)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.01)                # 10ms later: land the 2nd during teardown
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
        combined = (out + err).decode("utf-8", "replace")
        assert proc.returncode == 130, combined
        assert "Traceback" not in combined
    finally:
        if proc.poll() is None:         # failure path: don't leave the loop spinning forever
            proc.kill()
            proc.wait()


# --- in-process tests: drive main(argv) directly (W21) --------------------------------
#
# main() reads sys.stdin/stdout/stderr at call time, so monkeypatching those to StringIO
# both feeds stdin and captures the two output streams separately — letting us assert the
# frozen §6.2 routing: compile diagnostics -> stderr; program output, dis listing, and
# panic traces -> stdout.


def call_main(monkeypatch, argv, stdin_text: str = ""):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_version_prints_to_stdout(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["--version"])
    assert rc == 0
    assert re.match(r"weft \d+\.\d+\.\d+", out)
    assert err == ""


def test_version_short_flag(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["-V"])
    assert rc == 0
    assert re.match(r"weft \d+\.\d+\.\d+", out)


def test_unknown_command_exits_2_with_usage_on_stderr(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["frobnicate"])
    assert rc == 2
    assert out == ""
    assert "usage: weft" in err
    assert "unknown command" in err


@pytest.mark.parametrize("cmd", ["run", "check", "dis"])
def test_missing_file_argument_exits_2(monkeypatch, cmd):
    rc, out, err = call_main(monkeypatch, [cmd])
    assert rc == 2
    assert out == ""
    assert "missing file argument" in err


@pytest.mark.parametrize("cmd", ["run", "check", "dis"])
def test_valid_program_from_file(monkeypatch, tmp_path, cmd):
    prog = tmp_path / "hello.weft"
    prog.write_text('print("hi")\n')
    rc, out, err = call_main(monkeypatch, [cmd, str(prog)])
    assert rc == 0, err
    assert err == ""
    if cmd == "run":
        assert "hi" in out
    elif cmd == "dis":
        assert out != ""            # a bytecode listing on stdout
    else:                           # check succeeds silently
        assert out == ""


@pytest.mark.parametrize("cmd", ["run", "check", "dis"])
def test_valid_program_from_stdin(monkeypatch, cmd):
    rc, out, err = call_main(monkeypatch, [cmd, "-"], stdin_text='print("hi")\n')
    assert rc == 0, err
    assert err == ""
    if cmd == "run":
        assert "hi" in out
    elif cmd == "dis":
        assert out != ""
    else:
        assert out == ""


def test_check_error_routes_diagnostic_to_stderr(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["check", "-"], stdin_text="let x = undefined_name\n")
    assert rc == 2
    assert out == ""                # nothing on stdout for a compile error
    assert "check error" in err     # the diagnostic went to stderr
    assert "Traceback" not in err


def test_runtime_panic_routes_trace_to_stdout(monkeypatch):
    """A runtime panic exits 1; per the frozen §6.2 contract the panic trace and any
    program output go to stdout (the golden panic suite locks this), stderr stays empty."""
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="print(1 / 0)\n")
    assert rc == 1
    assert "panic" in out
    assert "R001" in out            # division-by-zero panic on stdout
    assert err == ""
    assert "Traceback" not in out


def test_stdin_file_label_from_file(monkeypatch, tmp_path):
    """Multi-module file path (build_program) and single-module stdin path agree on output."""
    prog = tmp_path / "sum.weft"
    prog.write_text("print(1 + 2)\n")
    rc, out, err = call_main(monkeypatch, ["run", str(prog)])
    assert rc == 0 and "3" in out and err == ""


@pytest.mark.parametrize("cmd", ["run", "check", "dis"])
def test_parse_error_from_stdin_routes_to_stderr(monkeypatch, cmd):
    """A single WeftError (parse error) renders to stderr for every stdin subcommand."""
    rc, out, err = call_main(monkeypatch, [cmd, "-"], stdin_text="let x = 1 +\n")
    assert rc == 2
    assert out == ""
    assert "parse error" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("cmd", ["run", "check", "dis"])
def test_nonexistent_file_exits_2(monkeypatch, tmp_path, cmd):
    rc, out, err = call_main(monkeypatch, [cmd, str(tmp_path / "nope.weft")])
    assert rc == 2
    assert out == ""
    assert "cannot read" in err


def test_latin1_file_exits_2_in_process(monkeypatch, tmp_path):
    bad = tmp_path / "bad.weft"
    bad.write_bytes(b'print("caf\xe9")\n')
    rc, out, err = call_main(monkeypatch, ["check", str(bad)])
    assert rc == 2
    assert "not valid UTF-8" in err


def test_bom_stdin_is_stripped(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text='﻿print("hi")\n')
    assert rc == 0, err
    assert "hi" in out


def test_no_args_starts_repl(monkeypatch):
    rc, out, err = call_main(monkeypatch, [], stdin_text="")
    assert rc == 0
    assert "REPL" in out


def test_old_python_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 11, 0))
    rc, out, err = call_main(monkeypatch, ["--version"])
    assert rc == 2
    assert "requires Python 3.12" in err


def test_keyboard_interrupt_boundary_returns_130(monkeypatch):
    """Ctrl-C during a command hits the dispatch boundary: exit 130, clean message."""
    import weft.cli as climod
    monkeypatch.setattr(climod, "run_text", _raiser(KeyboardInterrupt))
    original = signal.getsignal(signal.SIGINT)   # main()'s handler sets SIG_IGN in THIS process;
    try:                                         # restore after so later subprocess children (which
        rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="print(1)\n")  # inherit the
    finally:                                     # disposition) still see SIGINT, not an inherited SIG_IGN
        signal.signal(signal.SIGINT, original)
    assert rc == 130
    assert "interrupted" in err
    assert "Traceback" not in err


def test_internal_error_boundary_returns_2(monkeypatch):
    """Any other unexpected exception degrades to a one-line internal-error diagnostic."""
    import weft.cli as climod
    monkeypatch.setattr(climod, "run_text", _raiser(RuntimeError("synthetic bug")))
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="print(1)\n")
    assert rc == 2
    assert "internal error" in err
    assert "synthetic bug" in err
    assert "Traceback" not in err


def test_system_exit_propagates(monkeypatch):
    """SystemExit is re-raised, not swallowed by the catch-all boundary."""
    import weft.cli as climod
    monkeypatch.setattr(climod, "run_text", _raiser(SystemExit(7)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    monkeypatch.setattr(sys, "stdin", io.StringIO("print(1)\n"))
    with pytest.raises(SystemExit):
        main(["run", "-"])


def test_stdin_read_failure_returns_2(monkeypatch):
    """When the stdin read fails (invalid UTF-8), main exits 2 without dispatching."""
    import weft.cli as climod
    monkeypatch.setattr(climod, "_read_target", lambda *a, **k: None)
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="")
    assert rc == 2


# --- _read_target helper direct tests (its file branch is only reached via this API) ---


def test_read_target_reads_file(tmp_path):
    from weft.cli import _read_target
    f = tmp_path / "a.weft"
    f.write_text('print("x")\n')
    assert _read_target(str(f), io.StringIO()) == ('print("x")\n', str(f))


def test_read_target_missing_file():
    from weft.cli import _read_target
    err = io.StringIO()
    assert _read_target("/no/such/file.weft", err) is None
    assert "cannot read" in err.getvalue()


def test_read_target_latin1_file(tmp_path):
    from weft.cli import _read_target
    bad = tmp_path / "bad.weft"
    bad.write_bytes(b'print("caf\xe9")\n')
    err = io.StringIO()
    assert _read_target(str(bad), err) is None
    assert "not valid UTF-8" in err.getvalue()


# --- broken stdout pipe: silent exit 141, no traceback (DECISIONS.md D16) -------------


def _run_with_closed_stdout(prog_path):
    """Spawn `weft run <file>` with a piped stdout whose read end we close at once, so the
    child's output has nowhere to go — the shell equivalent of `weft run x | head` where the
    consumer quit early. Returns (returncode, stderr_text). stderr is drained to EOF (child
    exit) before waiting so a hung child can't deadlock the read."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "weft", "run", str(prog_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        proc.stdout.close()                 # the consumer went away
        err = proc.stderr.read()
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode, err.decode("utf-8", "replace")


def test_broken_pipe_small_output_exits_141_silently(tmp_path):
    """Small, buffered output: the broken pipe surfaces at main()'s explicit stdout flush.
    It must be a silent exit 141, never the shutdown-time 'Exception ignored ... BrokenPipeError'."""
    prog = tmp_path / "hi.weft"
    prog.write_text('print("hello")\n')
    rc, err = _run_with_closed_stdout(prog)
    assert rc == 141, err
    for bad in ("Traceback", "Exception ignored", "internal error"):
        assert bad not in err, err


def test_broken_pipe_large_output_exits_141_silently(tmp_path):
    """A program that prints far more than the OS pipe buffer (>64KB): the broken pipe is
    raised mid-stream inside a VM print — the path that used to be miscaught as an
    'internal error' exit 2. It must exit 141 with a clean stderr."""
    prog = tmp_path / "big.weft"
    prog.write_text(
        "fn main():\n"
        "    var i = 0\n"
        "    while i < 200000:\n"
        "        print(i)\n"
        "        i = i + 1\n"
        "\n"
        "main()\n"
    )
    rc, err = _run_with_closed_stdout(prog)
    assert rc == 141, err
    for bad in ("Traceback", "Exception ignored", "internal error"):
        assert bad not in err, err


# --- broken-pipe boundary completeness (D19): --version, stderr pipe, SIGINT+pipe ------


def _run_argv_closed_stdout(argv):
    """Spawn `weft <argv>` and close stdout's read end at once, like a consumer that quit.
    Drains stderr to EOF before waiting. Returns (returncode, stderr_text)."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "weft", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        proc.stdout.close()                 # the consumer went away
        err = proc.stderr.read()
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return proc.returncode, err.decode("utf-8", "replace")


def test_version_broken_stdout_pipe_exits_141_silently():
    """F1: `weft --version | closed-pipe`. The tiny version line is flushed inside main()'s
    boundary, so the broken pipe is a silent exit 141 — never a shutdown-time leak/exit 120."""
    rc, err = _run_argv_closed_stdout(["--version"])
    assert rc == 141, err
    for bad in ("Traceback", "Exception ignored", "internal error"):
        assert bad not in err, err


def test_check_error_broken_stderr_pipe_exits_141_silently(tmp_path):
    """F3: a compile error's diagnostic goes to stderr; if THAT pipe's read end is closed
    (`weft check bad.weft 2>&1 | head -0`), the flush must be a silent 141, not an exit-120 leak."""
    prog = tmp_path / "bad.weft"
    prog.write_text("let x = undefined_name\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "weft", "check", str(prog)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        proc.stderr.close()                 # the diagnostic consumer went away
        out = proc.stdout.read()
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    combined = out.decode("utf-8", "replace")
    assert proc.returncode == 141, combined
    for bad in ("Traceback", "Exception ignored", "internal error"):
        assert bad not in combined, combined


# --- std fds CLOSED at launch (not piped): CPython nulls the sys stream; exit codes must
# --- keep their documented meaning with the output simply discarded (D20) ---------------


def _run_shell_with_closed_fd(cmd_tail, redirect):
    """Run `python -m weft <cmd_tail>` through bash with a `2>&-`-style closed fd."""
    return subprocess.run(
        ["bash", "-c", f"'{sys.executable}' -m weft {cmd_tail} {redirect}"],
        capture_output=True, timeout=60,
    ).returncode


def test_closed_stderr_fd_successful_run_exits_0(tmp_path):
    prog = tmp_path / "ok.weft"
    prog.write_text('print("ok")\n')
    assert _run_shell_with_closed_fd(f"run '{prog}'", "2>&-") == 0


def test_closed_stderr_fd_check_error_exits_2(tmp_path):
    prog = tmp_path / "bad.weft"
    prog.write_text("let x = undefined_name\n")
    assert _run_shell_with_closed_fd(f"check '{prog}'", "2>&-") == 2


def test_closed_stdout_fd_successful_run_exits_0(tmp_path):
    prog = tmp_path / "ok.weft"
    prog.write_text('print("ok")\n')
    assert _run_shell_with_closed_fd(f"run '{prog}'", "1>&-") == 0


def test_sigint_with_broken_stdout_pipe_exits_130(tmp_path):
    """F4: Ctrl-C while stdout is a broken pipe. The interrupt wins (exit 130), and the
    KeyboardInterrupt handler silences the still-buffered stdout so its flush can't leak an
    'Exception ignored' at shutdown (the bug that turned this into exit 120). The program
    prints its marker once (buffered, never flushed) then spins silently, so no VM print
    ever hits the broken pipe first — keeping the exit deterministically 130, not 141."""
    prog = tmp_path / "loop.weft"
    prog.write_text(_LOOP_PROG)
    proc = subprocess.Popen(     # NOT -u: 'running\n' stays pending in the child's stdout buffer
        [sys.executable, "-m", "weft", "run", str(prog)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        proc.stdout.close()                 # break the pipe before any output is drained
        time.sleep(0.5)                     # let the child reach its silent loop
        assert proc.poll() is None, "child exited before the interrupt"
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=10)
        combined = err.decode("utf-8", "replace")
        assert proc.returncode == 130, combined
        assert "interrupted" in combined
        for bad in ("Traceback", "Exception ignored", "internal error"):
            assert bad not in combined, combined
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- CRLF/CR stdin is normalized to LF (DECISIONS.md D17) ------------------------------


def test_crlf_stdin_runs(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="let x = 1\r\nprint(x + 2)\r\n")
    assert rc == 0, err
    assert "3" in out


def test_cr_only_stdin_runs(monkeypatch):
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text="let x = 1\rprint(x + 4)\r")
    assert rc == 0, err
    assert "5" in out


# --- flat operator chain cap: a clean P003, never a RecursionError (DECISIONS.md D18) --


def test_long_flat_chain_from_stdin_is_p003_not_internal_error(monkeypatch):
    src = "let x = " + "1+" * 10_000 + "1\n"     # 10_001 operands, over the 10_000 cap
    rc, out, err = call_main(monkeypatch, ["check", "-"], stdin_text=src)
    assert rc == 2
    assert "P003" in err
    assert "expression too long" in err
    assert "internal error" not in err
    assert "Traceback" not in err


def test_long_flat_chain_within_cap_runs(monkeypatch):
    src = "print(" + "+".join(["1"] * 2000) + ")\n"
    rc, out, err = call_main(monkeypatch, ["run", "-"], stdin_text=src)
    assert rc == 0, err
    assert out.strip() == "2000"
    assert "internal error" not in err


def _raiser(exc):
    def _raise(*args, **kwargs):
        raise exc
    return _raise
