"""Shared test helpers: in-process pipeline execution + golden comparison.

Golden files are executed in-process (never via subprocess, DESIGN.md §8.1) using the CLI's
pipeline helpers with StringIO streams. The file label passed to the pipeline is the case's
basename so rendered paths in `.err`/`.out` are machine-independent (DESIGN.md §6.2).
"""

from __future__ import annotations

import io
import pathlib

from tantu.cli import compile_source, build_program
from tantu.vm import VM
from tantu.errors import TantuError, CheckErrors, render_error, render_errors

GOLDEN = pathlib.Path(__file__).parent / "golden"
ROOT = pathlib.Path(__file__).parent.parent


class _VirtualClock:
    """A deterministic clock: time only advances when a fiber sleeps (Fable M7 §5)."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += max(0.0, dt)


def run_capture(src: str, file: str, budget: int = 1000) -> tuple[str, str, int]:
    """Run source through the full pipeline on a virtual clock. Returns (stdout, stderr, exit)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        code = compile_source(src, file)
    except CheckErrors as e:
        err.write(render_errors(src, e.errors) + "\n")
        return out.getvalue(), err.getvalue(), 2
    except TantuError as e:
        err.write(render_error(src, e) + "\n")
        return out.getvalue(), err.getvalue(), 2
    clk = _VirtualClock()
    rc = VM(budget=budget, stdout=out, clock=clk.now, sleep_fn=clk.sleep).run(code, src, file)
    return out.getvalue(), err.getvalue(), rc


def run_program(main_path, budget: int = 1000) -> tuple[str, str, int]:
    """Discover + link + run a multi-module program rooted at `main_path` (file-based, M8)."""
    out, err = io.StringIO(), io.StringIO()
    built = build_program(str(main_path), err)
    if built is None:
        return out.getvalue(), err.getvalue(), 2
    entry, srcs = built
    clk = _VirtualClock()
    rc = VM(budget=budget, stdout=out, clock=clk.now, sleep_fn=clk.sleep).run(entry, srcs=srcs)
    return out.getvalue(), err.getvalue(), rc


def parse_directives(src: str) -> dict:
    """Parse a leading `# tantu-test: key=value[, ...]` header (DESIGN.md §8.1).

    v1 recognizes only `budget` (int). Unknown keys are an error. M1 does not use budget
    (single fiber, no preemption) but the header is validated for forward compatibility.
    """
    directives: dict = {}
    lines = src.splitlines()
    if not lines or not lines[0].startswith("# tantu-test:"):
        return directives
    body = lines[0][len("# tantu-test:"):].strip()
    for part in body.split(","):
        key, _, value = part.strip().partition("=")
        key = key.strip()
        if not key:
            continue
        if key == "budget":
            directives["budget"] = int(value)
        else:
            raise ValueError(f"unknown tantu-test directive key {key!r}")
    return directives


def check_or_update(path: pathlib.Path, actual: str, update: bool) -> None:
    """Compare `actual` to the golden at `path`, or (re)write it when `update` is set."""
    if update:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    assert path.exists(), f"missing golden {path} — regenerate with --update-goldens"
    expected = path.read_text()
    assert actual == expected, (
        f"golden mismatch for {path.name}:\n"
        f"--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )
