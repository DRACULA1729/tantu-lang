# Changelog

All notable changes to Tantu are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Tantu uses
[semantic versioning](https://semver.org/).

## 0.1.0 - 2026-07-10

First public release.

### Added

- The language: static types with no `null` (absence and failure are `Option[T]` and
  `Result[T, E]`), immutable-by-default bindings (`let`, with `var` to opt in), enums with
  exhaustive `match`, structs, closures, and user-defined generics (parametric, erased,
  inferred at the call site).
- Concurrency: `spawn`ed fibers that pass messages over typed channels, and `select` over
  several channel operations (source-order polling, an optional non-blocking `else` arm, a
  closed channel fires its recv arm with `None`).
- Modules: a program is a set of sibling `*.tn` files; `import name` exposes `name.member`.
  Imports are resolved, checked in topological order, and linked into one flat global table.
- A bytecode compiler and a hand-written stack VM with a cooperative, deterministic scheduler.
- A REPL, and a `tantu` command with `run`, `check`, and `dis` (disassemble) subcommands plus
  `--version`. `run -` reads a program from stdin.
- An append-only error-code registry (`tantu/errors.py`, mirrored in `DESIGN.md` Appendix C):
  `L` lex, `P` parse, `E` check, `R` runtime.
- Packaging: `pip install .` (or `pipx install .`) installs the `tantu` command. MIT license,
  and a GitHub Actions CI matrix (Linux/macOS, Python 3.12/3.13) running the test suite and
  every example.
- Zero third-party runtime dependencies.

### Hardening

This release closed a set of crash and soundness holes found in pre-release audits:

- No input (file, stdin, or a REPL line) produces a raw Python traceback. Everything degrades
  to a Tantu diagnostic or panic with the documented exit code; an internal error prints one
  line asking for a bug report.
- The parser guards nesting depth on every recursive entry point (`P003`).
- The VM caps call-stack depth (`R010`) and channel capacity is checked (`R011`); builtin
  domain errors are caught at the call site and reported as `R009` instead of crashing.
- Name hygiene: prelude and reserved names cannot be rebound, and duplicate declarations are
  rejected.
- The `?` operator and `return` are rejected inside an unannotated lambda rather than
  producing an unsound result.
- Expected types now flow into call arguments and list/tuple elements, so `Option`/`Result`
  values need fewer annotations.
- Values render iteratively (an explicit work stack), so printing a deeply nested value —
  a 5000-deep cons list, say — cannot overflow the interpreter stack.
- Scheduler leak fixes: finished fibers are pruned from the registry, and a winning
  `select` arm eagerly dequeues its sibling waiters from the other channels' wait queues.
