"""Smoke test: every program under examples/ must compile, link, and run to exit 0.

These are the showcase programs (README + DESIGN.md §9). They have no golden output —
the contract is simply that they stay runnable as the language evolves. A generous budget
covers the concurrency demos (channels, fibers) which take many scheduler steps.
"""

from __future__ import annotations

import pathlib

import pytest

from harness import run_program

EXAMPLES = sorted((pathlib.Path(__file__).parent.parent / "examples").glob("*.tn"))


@pytest.mark.parametrize("example", EXAMPLES, ids=[p.stem for p in EXAMPLES])
def test_example_runs(example):
    out, err, code = run_program(example, budget=100_000)
    assert err == "", f"{example.name} wrote to stderr:\n{err}"
    assert code == 0, f"{example.name} exited {code}, expected 0"
