"""End-to-end golden tests — the backbone suite (DESIGN.md §8.1).

run/   : program stdout, exit 0, no stderr.
error/ : rendered compile error on stderr, exit 2, no stdout.
panic/ : program output + panic trace on stdout, exit 1, no stderr.
"""

from __future__ import annotations

from harness import run_capture, run_program, parse_directives, check_or_update


def test_run_golden(run_case, update_goldens):
    src = run_case.read_text()
    budget = parse_directives(src).get("budget", 1000)
    out, err, code = run_capture(src, run_case.name, budget)
    assert err == "", f"unexpected stderr:\n{err}"
    assert code == 0, f"expected success, got exit {code}"
    check_or_update(run_case.with_suffix(".out"), out, update_goldens)


def test_error_golden(error_case, update_goldens):
    src = error_case.read_text()
    budget = parse_directives(src).get("budget", 1000)
    out, err, code = run_capture(src, error_case.name, budget)
    assert out == "", f"unexpected stdout:\n{out}"
    assert code == 2, f"expected compile-error exit 2, got {code}"
    check_or_update(error_case.with_suffix(".err"), err, update_goldens)


def test_panic_golden(panic_case, update_goldens):
    src = panic_case.read_text()
    budget = parse_directives(src).get("budget", 1000)
    out, err, code = run_capture(src, panic_case.name, budget)
    assert err == "", f"unexpected stderr:\n{err}"
    assert code == 1, f"expected panic exit 1, got {code}"
    check_or_update(panic_case.with_suffix(".out"), out, update_goldens)


def test_module_golden(module_case, update_goldens):
    """A multi-module program (golden/modules/<name>/main.tn + siblings) runs to exit 0."""
    out, err, code = run_program(module_case)
    assert err == "", f"unexpected stderr:\n{err}"
    assert code == 0, f"expected success, got exit {code}"
    check_or_update(module_case.parent / "expected.out", out, update_goldens)
