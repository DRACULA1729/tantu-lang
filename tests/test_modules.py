"""M8 unit tests: import parsing, cross-module resolution, and program discovery/linking.

Covers the pieces the golden `modules/` cases can't isolate: the parse shape of `import`,
each cross-module check error in isolation, and `build_program`'s discovery + cycle detection
(DESIGN.md §7 M8). Multi-file discovery is exercised against real files under `tmp_path`.
"""

from __future__ import annotations

import io

import pytest

from weft.lexer import tokenize
from weft.parser import parse
from weft.checker import check
from weft import ast_nodes as A
from weft.resolve import ImportedGlobal
from weft.cli import build_program
from weft.errors import (
    CheckErrors, WeftError,
    E_IMPORT_UNSUPPORTED, E_MODULE_NOT_FOUND, E_NOT_A_MODULE_MEMBER,
    E_MODULE_NOT_VALUE, E_IMPORT_CYCLE,
)


# --------------------------------------------------------------------- parsing

def test_import_parses_to_import_node():
    m = parse(tokenize("import mathlib\n", "<t>"))
    imports = [d for d in m.decls if isinstance(d, A.Import)]
    assert len(imports) == 1 and imports[0].name == "mathlib"


# ------------------------------------------------------- checker (in isolation)

def _check(src: str, imports=None):
    return check(parse(tokenize(src, "<t>")), imports=imports)


def _errors(src: str, imports=None) -> list:
    with pytest.raises(CheckErrors) as e:
        _check(src, imports=imports)
    return [x.code for x in e.value.errors]


def _mathlib_symbols() -> dict:
    """Check a tiny dependency and hand back its exported symbol table."""
    dep = _check("fn square(n: Int) -> Int:\n    n * n\nlet answer = 42\n")
    return dep.symbols


def test_import_without_program_context_is_unsupported():
    # single-module context (imports=None, e.g. stdin/REPL): import can't be honored
    assert E_IMPORT_UNSUPPORTED in _errors("import mathlib\n")


def test_import_of_unknown_module_is_not_found():
    assert E_MODULE_NOT_FOUND in _errors("import nope\nlet x = 1\n", imports={})


def test_qualified_member_resolves_to_imported_global():
    syms = _mathlib_symbols()
    m = _check("import mathlib\nlet r = mathlib.square(3)\n", imports={"mathlib": syms})
    # find the `mathlib.square` field access and confirm it resolved to the dep's slot
    binding = m.decls[1]
    call = binding.value
    field = call.func
    assert isinstance(field.res, ImportedGlobal)
    assert field.res.module == "mathlib"
    assert field.res.slot == syms["square"][0]


def test_unknown_member_is_reported():
    syms = _mathlib_symbols()
    assert E_NOT_A_MODULE_MEMBER in _errors(
        "import mathlib\nlet x = mathlib.nope\n", imports={"mathlib": syms})


def test_module_name_as_bare_value_is_error():
    syms = _mathlib_symbols()
    assert E_MODULE_NOT_VALUE in _errors(
        "import mathlib\nlet x = mathlib\n", imports={"mathlib": syms})


# ----------------------------------------------- build_program (real files)

def _write(dir_, name: str, body: str):
    p = dir_ / name
    p.write_text(body)
    return p


def test_build_program_links_and_runs(tmp_path):
    _write(tmp_path, "lib.weft", "fn dbl(n: Int) -> Int:\n    n + n\nlet base = 10\n")
    main = _write(tmp_path, "main.weft",
                  "import lib\nfn main() -> Unit:\n    print(lib.dbl(lib.base))\n")
    err = io.StringIO()
    built = build_program(str(main), err)
    assert built is not None, err.getvalue()
    entry, srcs = built
    assert "main.weft" in srcs and "lib.weft" in srcs


def test_build_program_missing_module(tmp_path):
    main = _write(tmp_path, "main.weft", "import ghost\nlet x = 1\n")
    err = io.StringIO()
    assert build_program(str(main), err) is None
    assert E_MODULE_NOT_FOUND in err.getvalue()


def test_build_program_detects_cycle(tmp_path):
    _write(tmp_path, "a.weft", "import b\nlet x = 1\n")
    _write(tmp_path, "b.weft", "import a\nlet y = 2\n")
    err = io.StringIO()
    assert build_program(str(tmp_path / "a.weft"), err) is None
    assert E_IMPORT_CYCLE in err.getvalue()
