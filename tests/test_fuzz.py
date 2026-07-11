"""Seeded fuzz (DESIGN.md §8.1 point 5), zero new deps, fixed seeds for CI determinism.

(a) The lexer over random inputs must either raise LexError or return a token list ending in
    EOF — never a stray Python exception, never a silent crash (the MISMATCH lesson, §9).
(b) Parsing is deterministic: the same generated program yields the same pretty() every time.
"""

from __future__ import annotations

import random
import string

from tantu.lexer import tokenize
from tantu.parser import parse
from tantu.tokens import TokenType
from tantu.errors import TantuError


def test_lexer_never_crashes_on_random_input():
    rng = random.Random(1234)
    for _ in range(3000):
        n = rng.randint(0, 48)
        s = "".join(rng.choice(string.printable) for _ in range(n))
        try:
            toks = tokenize(s, "<fuzz>")
        except TantuError:
            continue  # a clean, expected failure
        assert toks[-1].type is TokenType.EOF


def _gen_expr(rng: random.Random, depth: int) -> str:
    if depth <= 0 or rng.random() < 0.3:
        return str(rng.randint(0, 999))
    op = rng.choice(["+", "-", "*", "/", "%"])
    return f"({_gen_expr(rng, depth - 1)} {op} {_gen_expr(rng, depth - 1)})"


def test_parse_is_deterministic():
    rng = random.Random(99)
    for _ in range(500):
        src = "print(" + _gen_expr(rng, 4) + ")\n"
        p1 = parse(tokenize(src, "<fuzz>")).pretty()
        p2 = parse(tokenize(src, "<fuzz>")).pretty()
        assert p1 == p2
