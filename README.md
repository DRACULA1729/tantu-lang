# Weft

[![CI](https://github.com/DRACULA1729/weft-lang/actions/workflows/ci.yml/badge.svg)](https://github.com/DRACULA1729/weft-lang/actions/workflows/ci.yml)

A statically-typed, Python-readable language with fibers and channels — compiled to
bytecode and run on a hand-built stack VM, implemented in Python 3.12 with zero
third-party runtime dependencies.

- **Design contract:** [`DESIGN.md`](DESIGN.md) (v1.2) is the single source of truth.
- **Decisions log:** [`DECISIONS.md`](DECISIONS.md) records anything the design leaves silent.
- **Status:** M1–M9 complete — a full pipeline (lexer → parser → checker → compiler → linker → stack VM),
  static types with no null (`Option`/`Result`), pattern matching, closures, fibers + channels
  with `select`, multi-file modules, and user-defined generics. The full test suite and all
  examples run in CI.

See [`CHANGELOG.md`](CHANGELOG.md) for release notes and [`CONTRIBUTING.md`](CONTRIBUTING.md)
if you want to hack on it.

## A taste

```
enum Shape:
    Circle(Float)
    Rect(Float, Float)

fn area(s: Shape) -> Float:
    match s:
        Circle(r) => 3.14159 * r * r
        Rect(w, h) => w * h

fn main() -> Unit:
    print(area(Circle(2.0)))
    print(area(Rect(3.0, 4.0)))
```

No `null`: absence and failure are values (`Option[T]`, `Result[T, E]`) unwrapped by `match`
or the `?` operator. Bindings are immutable by default (`let`; `var` opts in). Concurrency is
CSP — lightweight `spawn`ed fibers passing messages over typed `chan[T]` channels, never shared
mutable state. See [`examples/`](examples/) for runnable programs (arithmetic, closures,
collections, options, fizzbuzz, word-frequency, channels, shapes, select, generics).

`select` waits on several channel operations at once and takes the first ready arm (a recv
arm binds `Option[T]`; a closed channel makes its recv arm fire with `None`). With no ready
arm it blocks until one is; an `else` arm makes it non-blocking. The canonical use is a
timeout — race real work against a fiber that sleeps then signals:

```
select:
    v = recv(result) =>
        match v:
            Some(n) => print(n)
            None => print("closed")
    deadline = recv(timeout) =>
        print("timed out")
```

Ready arms are polled in source order, so scheduling stays deterministic and testable.

## Install

Weft needs Python 3.12 or newer and installs a `weft` command.

```
pip install .                                              # from a clone
pipx install git+https://github.com/DRACULA1729/weft-lang  # isolated, without a clone
```

## Usage

```
weft run file.weft     # run a program (use `run -` to read stdin)
weft check file.weft   # static check only
weft dis file.weft     # disassemble to bytecode
weft                   # REPL
weft --version
```

If you're working from a checkout without installing, `python -m weft ...` takes the same
arguments.

## Modules

A program is a set of sibling `*.weft` files in one directory. `import name` makes the
members of `name.weft` available as `name.member`:

```
# mathlib.weft
fn square(n: Int) -> Int:
    n * n
let answer = 42

# main.weft
import mathlib
fn main() -> Unit:
    print(mathlib.square(5))   # 25
    print(mathlib.answer)      # 42
```

Running `weft run main.weft` discovers the import graph from the root, checks each module in
topological order, links every module's globals into one flat table, runs each module's
top-level initializers (dependencies first), then calls the root's `main()`. Import cycles and
missing modules are compile-time errors. (`import` needs a source file on disk — it isn't
available for stdin or the REPL.)

## Generics

Functions, enums, and structs can take type parameters in `[ ]`. Generics are fully
parametric (no bounds) and **erased** — a generic definition compiles to a single function or
descriptor, and type arguments are always inferred from the call, never written explicitly:

```
fn map[T, U](xs: List[T], f: (T) -> U) -> List[U]:
    var out: List[U] = []
    for x in xs:
        out = push(out, f(x))
    out

enum Tree[T]:
    Leaf
    Node(Tree[T], T, Tree[T])

struct Pair[A, B]:
    first: A
    second: B
```

Inside a generic body a type parameter is opaque: it can be passed, stored, matched, and sent
over a channel, but not added, compared, or called — there is nothing the checker knows it
supports. A generic function is call-only (not a first-class value); pass a lambda with
concrete types if you need one as a value.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```
