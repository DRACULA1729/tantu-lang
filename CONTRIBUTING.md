# Contributing to Weft

Thanks for looking. Weft is a small codebase with a clear contract, so most changes are
straightforward once you know where things live.

## Setup

You need Python 3.12 or newer.

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`pytest` runs the whole suite. The CI job runs the same suite plus `weft check` and
`weft run` over every file in `examples/`.

## Where things live

The `weft/` package is a pipeline, one stage per module:

```
lexer.py -> parser.py -> checker.py -> compiler.py -> vm.py
```

`tokens.py`, `ast_nodes.py`, `wtypes.py`, `resolve.py`, `bytecode.py`, and `values.py` hold the
data types passed between stages — `bytecode.py` defines the `Op` opcode set and `Instr`/`Code`
the compiler emits and the VM executes. `exhaustive.py` is the match-exhaustiveness check, `linker.py` flattens
multi-module programs, `prelude.py` defines the builtins, `errors.py` is the error hierarchy
and the code registry, and `cli.py` / `repl.py` / `__main__.py` are the entry points.

Tests live in `tests/`. Alongside the unit tests there are golden tests: a `.weft` program and
its recorded output. They live under `tests/golden/` in `run/` (stdout, exit 0), `error/`
(rendered compile error on stderr, exit 2), `panic/` (output plus a panic trace, exit 1), and
`modules/` (one directory per multi-module program). Adding a case is dropping a `.weft` file
into the right directory; the harness discovers it.

To update the recorded output after an intentional change, run:

```
pytest --update-goldens
```

That rewrites the `.out`/`.err` files from the current behavior. Read the diff before you
commit it — an unexpected change in a golden usually means a real regression.

## The contract

`DESIGN.md` (v1.2) is the specification. If the implementation and `DESIGN.md` disagree, one of
them is a bug; say which in your change. Anything the design leaves open is recorded in
`DECISIONS.md` (append-only — add a row, never edit one).

Error codes in `weft/errors.py` are append-only and never reused. Add a new code; don't
renumber or recycle an old one. `DESIGN.md` Appendix C and `docs/errors.md` list them all.

## Reporting bugs

Open an issue with:

- the output of `weft --version`,
- the `.weft` program (the smallest one that still shows it),
- what you expected, and
- what you got.

A raw Python traceback from any input is always a bug. Please include the full traceback.

## Pull requests

- The test suite passes (`pytest`) and every example still runs.
- New behavior comes with a test, usually a golden.
- A user-visible change touches `DESIGN.md` / `DECISIONS.md` / `CHANGELOG.md` as needed.
