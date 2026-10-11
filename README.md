<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/tantu-file-dark.svg">
    <img src="assets/tantu-file-light.svg" width="96" alt="Tantu logo">
  </picture>
</p>

# Tantu

[![CI](https://github.com/DRACULA1729/tantu-lang/actions/workflows/ci.yml/badge.svg)](https://github.com/DRACULA1729/tantu-lang/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tantu)](https://pypi.org/project/tantu/)

A typed language with green threads, a type checker and a bytecode VM, in about 6,000 lines
of plain Python you can read in a weekend.

**Try it in your browser** at [tantu-play.netlify.app/play](https://tantu-play.netlify.app/play/),
with nothing to install, or run `pip install tantu` (Python 3.12+).

Tantu has static types with no null, pattern matching, closures, generics, multi-file modules,
and fibers that talk over channels with `select`. The whole pipeline is
lexer → parser → checker → compiler → linker → stack VM. Each stage is one small module with
no third-party dependencies, so you can read the whole thing front to back.
[Read the implementation](#read-the-implementation) gives you the order.

- **Design contract:** [`DESIGN.md`](DESIGN.md) (v1.2) is the single source of truth.
- **Decisions log:** [`DECISIONS.md`](DECISIONS.md) records anything the design leaves silent.
- **Release notes:** [`CHANGELOG.md`](CHANGELOG.md).

## Why Tantu

Every language I like makes me give something up. Python reads like pseudocode but
catches nothing before it runs and has no concurrency primitives of its own. Go gives
you cheap goroutines, then shares memory by default and only *asks* you not to race —
the detector is opt-in and finds races at runtime, if you're lucky. Rust proves your
concurrency is safe but charges you the borrow checker for the proof.

Tantu is a small answer to a narrow question: can a language read like Python, catch
your mistakes before it runs, and make data races impossible without a borrow checker?

It gets there with three constraints:

- **No null.** Absence and failure are ordinary values (`Option`, `Result`), and the
  checker won't let you ignore them. `match` and the `?` operator are the only ways through.
- **Immutable by default.** `let` bindings and every collection are values; `push`
  returns a new list. `var` buys you a reassignable binding and nothing more.
- **No shared mutable state across fibers.** Capturing a `var` in a closure or a
  spawned fiber is a compile error. Fibers talk over typed channels, never shared memory.

That third rule is why Tantu exists. A Tantu program can't contain a data race — not
because you were careful, but because the language took away the tools to write one.
You get Go's concurrency ergonomics (`spawn`, `chan`, `select`) with a guarantee Go
doesn't give you, for the price of one checker rule instead of an ownership system.

### When to use it, and when not to

Use Tantu if you want a tiny statically-typed language with real concurrency that you
can read end to end — for scripts, for exercises, or to see how these pieces fit
together. The whole thing is pure Python with no runtime dependencies, and each stage
(lexer, parser, checker, compiler, stack VM) is a small module you can read on its own.
It's as much something to study as something to run.

Don't use it if you need speed or a library ecosystem. It runs on a bytecode VM written
in Python, with a cooperative, deterministic scheduler; it's built for correctness and
clarity, not throughput. v1 is deliberately small: erased generics, one concurrency
model, one way to handle errors. Need performance? Use Go or Rust. Need libraries? Use
Python. Tantu is for when you'd rather have a language you can hold in your head, with
the safety properties above spelled out.

## Read the implementation

If you want to see how a real language works end to end, read the modules in this order.
Each file opens with a docstring that says what it does and which part of
[`DESIGN.md`](DESIGN.md) it implements.

| # | Stage | Files | Lines | What to look at |
|---|---|---|---|---|
| 1 | Lexing | [`tokens.py`](tantu/tokens.py), [`lexer.py`](tantu/lexer.py) | 368 | How INDENT/DEDENT tokens come from an indent stack, Python style |
| 2 | Parsing | [`parser.py`](tantu/parser.py), [`ast_nodes.py`](tantu/ast_nodes.py) | 1,412 | A hand-written recursive-descent parser with a precedence ladder |
| 3 | Checking | [`resolve.py`](tantu/resolve.py), [`checker.py`](tantu/checker.py), [`ttypes.py`](tantu/ttypes.py) | 1,802 | Type inference, erased generics, and the one rule that makes data races impossible (no `var` captured by a closure or fiber) |
| 4 | Exhaustiveness | [`exhaustive.py`](tantu/exhaustive.py) | 165 | Maranget's pattern-matrix algorithm: missing-case witnesses and unreachable arms |
| 5 | Compiling | [`compiler.py`](tantu/compiler.py), [`bytecode.py`](tantu/bytecode.py) | 686 | AST straight to stack bytecode, with no IR and no optimizer |
| 6 | Linking | [`linker.py`](tantu/linker.py) | 86 | How separately compiled modules share one global table |
| 7 | Running | [`vm.py`](tantu/vm.py), [`values.py`](tantu/values.py) | 714 | The stack VM and a deterministic, cooperative fiber scheduler with channels and timers |

Errors from every stage go through one renderer in [`errors.py`](tantu/errors.py). The
builtins live in [`prelude.py`](tantu/prelude.py).

To watch the stages work on your own code, open the
[playground](https://tantu-play.netlify.app/play/) and switch between the Tokens, AST and
Bytecode tabs. The [Under the hood](https://tantu-play.netlify.app/guide/under-the-hood) page
of the guide walks through the same pipeline in prose.

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
mutable state. See [`examples/`](examples/) for runnable programs (hello, arithmetic, closures,
collections, options, fib, fizzbuzz, word-frequency, channels, shapes, select, generics).

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

Tantu needs Python 3.12 or newer and installs a `tantu` command.

```
pip install tantu     # from PyPI (or: pipx install tantu)
pip install .         # from a clone
```

## Usage

```
tantu run file.tn     # run a program (use `run -` to read stdin)
tantu check file.tn   # static check only
tantu dis file.tn     # disassemble to bytecode
tantu                   # REPL
tantu --version
```

If you're working from a checkout without installing, `python -m tantu ...` takes the same
arguments.

## Modules

A program is a set of sibling `*.tn` files in one directory. `import name` makes the
members of `name.tn` available as `name.member`:

```
# mathlib.tn
fn square(n: Int) -> Int:
    n * n
let answer = 42

# main.tn
import mathlib
fn main() -> Unit:
    print(mathlib.square(5))   # 25
    print(mathlib.answer)      # 42
```

Running `tantu run main.tn` discovers the import graph from the root, checks each module in
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

## Contributing

Want to help? Start with an issue labelled
[good first issue](https://github.com/DRACULA1729/tantu-lang/labels/good%20first%20issue).
Each one names the files to change and the test that proves it works.
[`CONTRIBUTING.md`](CONTRIBUTING.md) explains the setup, the golden tests and the PR rules.
