"""pytest configuration: --update-goldens option + golden-directory parametrization.

Adding a golden case = dropping a `<name>.weft` (and its expected `.out`/`.err`) into the
right directory; `pytest_generate_tests` auto-discovers it (DESIGN.md §8.1).
"""

from __future__ import annotations

import pathlib

import pytest

GOLDEN = pathlib.Path(__file__).parent / "golden"


def pytest_addoption(parser):
    parser.addoption("--update-goldens", action="store_true", default=False,
                     help="regenerate golden .out/.err/.txt files instead of asserting")


@pytest.fixture
def update_goldens(request) -> bool:
    return request.config.getoption("--update-goldens")


def _cases(subdir: str) -> list[pathlib.Path]:
    d = GOLDEN / subdir
    return sorted(d.glob("*.weft")) if d.exists() else []


def _module_cases() -> list[pathlib.Path]:
    """Each multi-module golden is a directory under golden/modules/ holding a `main.weft`."""
    d = GOLDEN / "modules"
    return sorted(p for p in d.glob("*/main.weft")) if d.exists() else []


def pytest_generate_tests(metafunc):
    for fixture, subdir in (("run_case", "run"), ("error_case", "error"), ("panic_case", "panic")):
        if fixture in metafunc.fixturenames:
            cases = _cases(subdir)
            metafunc.parametrize(fixture, cases, ids=[p.stem for p in cases])
    if "module_case" in metafunc.fixturenames:
        cases = _module_cases()
        metafunc.parametrize("module_case", cases, ids=[p.parent.name for p in cases])
