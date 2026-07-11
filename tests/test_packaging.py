"""Packaging smoke tests: the module entry point works and reports the version."""

import subprocess
import sys

from tantu import __version__


def test_python_m_tantu_version() -> None:
    """`python -m tantu --version` exits 0 and prints the package version."""
    result = subprocess.run(
        [sys.executable, "-m", "tantu", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"exit code {result.returncode}; stderr: {result.stderr!r}"
    )
    assert __version__ in result.stdout, (
        f"expected version {__version__!r} in output, got: {result.stdout!r}"
    )
    assert "tantu" in result.stdout
