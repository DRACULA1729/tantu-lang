"""VM.run(step_limit=...) — the optional total-instruction cap (R008).

Backward-compatible: default (no step_limit) is unlimited and unchanged; a set limit turns an
otherwise-forever program into a graceful panic instead of a hang. Used by the web playground to
stop runaway `loop:` code in-band rather than killing the Pyodide worker.
"""

from __future__ import annotations

import io

from tantu.cli import compile_source
from tantu.errors import R_STEP_LIMIT
from tantu.vm import VM

from harness import _VirtualClock

_FOREVER = "fn main():\n    loop:\n        let x = 1\n"


def _run(src: str, *, step_limit=None, budget: int = 1000):
    out = io.StringIO()
    clk = _VirtualClock()
    code = compile_source(src, "<t>")
    rc = VM(budget=budget, stdout=out, clock=clk.now, sleep_fn=clk.sleep).run(
        code, src, "<t>", step_limit=step_limit)
    return out.getvalue(), rc


def test_infinite_loop_hits_step_limit():
    out, rc = _run(_FOREVER, step_limit=500)
    assert rc == 1                                   # panic exit code
    assert f"[{R_STEP_LIMIT}]" in out                # R008 rendered to stdout
    assert "execution step limit reached" in out


def test_step_limit_none_is_unlimited_for_finite_program():
    # A normal terminating program is unaffected when no limit is set.
    src = "fn main():\n    print(6 * 7)\n"
    out, rc = _run(src, step_limit=None)
    assert (out, rc) == ("42\n", 0)


def test_generous_step_limit_does_not_disturb_finite_program():
    src = "fn main():\n    print(6 * 7)\n"
    out, rc = _run(src, step_limit=10_000)
    assert (out, rc) == ("42\n", 0)
