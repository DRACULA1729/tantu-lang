"""Packaging smoke tests: the module entry point works and reports the version."""

import subprocess
import sys

from weft import __version__


def test_python_m_weft_version() -> None:
    """`python -m weft --version` exits 0 and prints the package version."""
    result = subprocess.run(
        [sys.executable, "-m", "weft", "--version"],
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
    assert "weft" in result.stdout
