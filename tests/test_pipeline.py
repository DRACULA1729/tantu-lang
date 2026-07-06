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


def _run(src: str) -> str:
    """Drive src through all five stages; assert exit 0 and return stdout."""
    module = parser.parse(lexer.tokenize(src, "<t>"))
    code = compiler.compile_module(checker.check(module))
    out = io.StringIO()
    rc = vm.VM(stdout=out).run(code, src, "<t>")
    assert rc == 0
    return out.getvalue()


# -- W9: unit literal () -------------------------------------------------------

def test_unit_literal_end_to_end():
    assert _run("let u = ()\nprint(u)\n") == "()\n"
    assert _run("print(())\n") == "()\n"


# -- W10: statement-position + block-form if-expressions ------------------------

def test_statement_position_if_expr_end_to_end():
    src = ('fn find_user(id: Int) -> Option[Str]:\n'
           '    if id == 1: Some("ada") else: None\n'
           'print(find_user(1))\n'
           'print(find_user(2))\n')
    assert _run(src) == 'Some("ada")\nNone\n'


def test_block_form_if_expr_end_to_end():
    src = ("let x = 3\n"
           "let y = if x > 0:\n"
           "    let z = x * 2\n"
           "    z + 1\n"
           "else:\n"
           "    0\n"
           "print(y)\n")
    assert _run(src) == "7\n"


def test_inline_if_expr_elif_end_to_end():
    src = ('let score = 85\n'
           'let grade = if score >= 90: "A" elif score >= 80: "B" else: "C"\n'
           'print(grade)\n')
    assert _run(src) == "B\n"
