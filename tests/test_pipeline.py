"""Pipeline smoke test — the anti-interface-drift tripwire (DESIGN.md §8.1 point 4).

Imports every module (catching any stdlib-shadow / import-time break) and drives one program
through the five §6.2 public functions, asserting the type at each stage boundary.
"""

from __future__ import annotations

import io

from weft import (  # noqa: F401 — importing is the point (no import-time break, no shadow)
    lexer, parser, checker, compiler, vm, cli, repl,
    tokens, ast_nodes, bytecode, errors, values, wtypes,
)
from weft.tokens import Token, TokenType
from weft.ast_nodes import Module
from weft.bytecode import Code


def test_stage_boundaries():
    src = 'print("hi")\n'

    toks = lexer.tokenize(src, "<smoke>")           # 1. Lexer -> list[Token]
    assert isinstance(toks, list) and all(isinstance(t, Token) for t in toks)
    assert toks[-1].type is TokenType.EOF

    module = parser.parse(toks)                       # 2. Parser -> Module
    assert isinstance(module, Module)

    checked = checker.check(module)                   # 3. Checker -> same Module, annotated
    assert checked is module
    assert module.decls[0].expr.ty is not None

    code = compiler.compile_module(checked)           # 4. Compiler -> Code
    assert isinstance(code, Code)

    out = io.StringIO()                               # 5. VM -> exit status + stdout
    rc = vm.VM(stdout=out).run(code, src, "<smoke>")
    assert rc == 0
    assert out.getvalue() == "hi\n"
