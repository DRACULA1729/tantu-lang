# Tantu — Language & Toolchain Design (v1.2)

> **Status**: LOCKED design contract for the implementation phase.
> **v1.1**: consistency refinement of v1 — fixed contradictions between examples, grammar, and
> ISA; specified the parking protocol, block typing, entry point, and REPL contract; corrected
> effort estimates. One surface change: lambdas may now have block bodies.
> **v1.2**: spec brought up to the shipped language — `select` documented (§4.6, grammar,
> ISA); the M9b generic parameter lists added to the §2.11 grammar; Appendix C refreshed to
> the full shipped error-code registry (new codes P003, L006, R009, R010, R011); `inf`/`nan`
> rendering locked (Appendix B); REPL continuation rule clarified (§6.4).
> **Host**: Python 3.12, zero third-party runtime dependencies (pytest for tests only).
> **Execution model**: source → lexer → parser → AST → type checker → bytecode compiler → stack VM.
> This document is the single source of truth. Where it is silent, choose the simplest thing
> consistent with its decisions and record the choice in `DECISIONS.md` — an append-only table
> at the repo root beside this file: `date | milestone | decision (one line) | why (one line)`.

---

## 1. Language Identity

### Name: **Tantu**

*Tantu* (Sanskrit तन्तु) is the word for a thread or fiber — from the root *tan*, "to stretch",
the same root that gives *tantra*, the loom. Tantu is a language where threads are woven
in, not bolted on: lightweight fibers and channels are language primitives, the type system
refuses to let null or unhandled errors through, and everything reads like pseudocode.

**Philosophy.** Tantu blends four things that rarely coexist: Python's visual clarity
(indentation, no ceremony), Rust/TypeScript's compile-time safety (static types, no null —
`Option`/`Result` instead), Go's concurrency ergonomics (spawn a fiber, talk over a channel),
and a functional core (immutable by default, pattern matching, expression-oriented,
first-class functions). It is deliberately small: monomorphic types in v1, one concurrency
model, one way to handle errors. Correctness and clarity beat speed everywhere. The
implementation is itself a teaching artifact — every pipeline stage is a small, separately
testable Python module with an explicit data contract.

File extension: `.tn`. CLI: `python -m tantu run file.tn` (`run -` reads stdin),
`python -m tantu check file.tn` (static check only), `python -m tantu` (REPL),
`python -m tantu dis file.tn` (disassembler), `python -m tantu --version`.

### Hello world

```tantu
fn main() -> Unit:
    print("hello, world")
```

### Showcase (real committed syntax — types, pattern matching, functions, concurrency)

```tantu
enum Shape:
    Circle(Float)
    Rect(Float, Float)

fn area(s: Shape) -> Float:
    match s:
        Circle(r)  => 3.14159 * r * r
        Rect(w, h) => w * h

fn worker(jobs: Chan[Shape], out: Chan[Float]) -> Unit:
    loop:
        match recv(jobs):            # recv returns Option[Shape]; None once closed+drained
            Some(s) => send(out, area(s))
            None    => break

fn main() -> Unit:
    let jobs = chan[Shape](8)        # buffered channel, capacity 8
    let out  = chan[Float](8)
    spawn worker(jobs, out)
    send(jobs, Circle(1.0))
    send(jobs, Rect(3.0, 4.0))
    close(jobs)
    print(recv(out))                 # Some(3.14159)
    print(recv(out))                 # Some(12.0)
```

---

## 2. Syntax Specification (v1)

### 2.1 Decision: indentation-based blocks

**Committed: indentation-based (Python-style), with INDENT/DEDENT tokens produced by the
hand-written lexer.**

Rationale: "reads like pseudocode" is the identity of the language and a locked requirement;
braces would surrender it. The cost is one well-understood lexer feature (~50 lines: an
indent stack, exactly the algorithm CPython documents). It is far cheaper than it looks, and
it eliminates the failure mode that killed old attempt #1, where the tokenizer emitted
indentation but the parser expected `{}`. Rules that keep it simple:

- Indentation is **spaces only**. A tab character at the start of a line is a lex error
  (`LexError: tabs are not allowed in indentation`). No tab-width debates, ever.
- Each indent level must be a strict increase; each DEDENT must return to a level already on
  the stack, else `LexError: unindent does not match any outer indentation level`.
- Blank lines and comment-only lines emit no NEWLINE/INDENT/DEDENT.
- Inside unclosed `(` `[` brackets, newlines and indentation are ignored (implicit line
  joining), tracked by a bracket-depth counter in the lexer.
- EOF closes all open blocks (emits pending DEDENTs, then EOF).

### 2.2 Comments, identifiers, keywords

- Comments: `#` to end of line. No block comments.
- Identifiers: `[a-zA-Z_][a-zA-Z0-9_]*`. Convention (not enforced): types and enum variants
  are `CamelCase`, values/functions `snake_case`.
- Keywords (reserved): `fn let var if elif else while for in loop break continue return
  match enum struct spawn select import and or not true false`.
  Note: `print`, `chan`, `send`, `recv`, `close`, `sleep` are **prelude intrinsics** (compiled
  to dedicated opcodes), not keywords. `None` is likewise not a keyword — it is a prelude enum
  variant. `chan` lexes as an ordinary IDENT; the parser special-cases a `primary` that begins
  with the identifier `chan` followed by `[` (safe because `chan` cannot be shadowed).
- **No prelude name may be rebound or shadowed** in v1 (checker error). Intrinsics may appear
  only as the callee of a call — using one as a value is a compile error
  (`intrinsic 'print' is not a first-class value`). Builtins (`str`, `len`, `push`, …) are
  likewise **not** first-class values: referencing any builtin outside call position is a
  checker error (E0109, e.g. `let f = float`). Call it, or wrap it in a lambda
  (`fn(x: Int): print(x)`).

### 2.3 Literals & primitive types

| Type    | Literals                       | Notes |
|---------|--------------------------------|-------|
| `Int`   | `0`, `42`, `1_000_000`         | arbitrary precision (host int). `/`, `%` are floor-based (like Python); div/mod by zero panics. |
| `Float` | `3.14`, `0.5`, `1e-3`          | host float. No implicit Int↔Float conversion — mixing is a type error; use `float(n)` / `trunc(x)`. `+ - * / %` and comparisons follow host IEEE-754 semantics: a `0.0` divisor panics; overflow yields `inf`; NaN compares unequal to everything, including itself. |
| `Bool`  | `true`, `false`                | |
| `Str`   | `"hi\n"` (double quotes only)  | immutable; escapes `\n \t \\ \" \r`; concatenation with `+`; ordering is lexicographic by Unicode code point; no interpolation in v1 (use `str(x)` + `+`). |
| `Unit`  | `()`                           | the "no value" value; type of statements and of functions with no `-> R`. |

### 2.4 Bindings — immutable by default

```tantu
let x = 10                 # immutable, type inferred: Int
let name: Str = "tantu"     # optional annotation (required where inference can't decide)
var count = 0              # mutable — the ONLY way to get mutability
count = count + 1          # reassignment: allowed only for `var`; `=` on a `let` is a compile error
```

- Shadowing with a new `let` in an inner scope is allowed; re-`let` in the same scope is an
  error. (REPL exception: each REPL input is checked as a new nested scope, so re-`let` of a
  top-level name is allowed at the prompt and shadows the previous binding — see §6.4.)
- Collections are **immutable values**: `push(xs, x)` returns a *new* list. There is no
  in-place mutation of any compound value in v1. `var` only makes the *binding* reassignable.
- Closures may capture `let` bindings (by value — safe, they're immutable). **Capturing a
  `var` in a closure or spawned fiber is a compile-time error** in v1
  (`cannot capture mutable binding 'count'; copy it into a 'let' first`). This removes cell
  variables from the VM and removes shared-memory races from the concurrency model in one stroke.

### 2.5 Functions

```tantu
fn add(a: Int, b: Int) -> Int:
    a + b                          # last expression of the body is the return value

fn log(msg: Str):                  # omitted return type means -> Unit
    print("[log] " + msg)

fn apply(f: (Int) -> Int, x: Int) -> Int:
    f(x)

let double = fn(x: Int) -> Int: x * 2      # anonymous fn, inline expression body

let clamp = fn(x: Int) -> Int:             # anonymous fn, block body (same rules as named fns)
    if x < 0:
        0
    else:
        x
```

- Parameter types and (non-Unit) return types are **required annotations**. This is the
  inference boundary: signatures are explicit, bodies are inferred.
- `return expr` allowed anywhere for early return; `return` alone returns `()`.
- Anonymous `fn` bodies are either a single inline expression or an indented block on the
  following lines, exactly like named functions.
- Functions are first-class values with type `(T1, T2) -> R`.
- Recursion works; module-level functions may call each other regardless of order
  (two-pass checking: signatures first, then bodies).

### 2.6 Control flow — expression-oriented

```tantu
let grade = if score >= 90: "A" elif score >= 80: "B" else: "C"

let y = if x > 0:                  # block-form if in expression position:
    let z = x * 2                  # the "if" must start on the same line as "="
    z + 1                          # block value = last expression
else:
    0

while n > 0:                       # loops are statements (type Unit)
    n = n - 1

for item in items:                 # items: List[T]; item is an immutable let per iteration
    print(item)

loop:                              # infinite loop; exit with break
    if done(): break
```

- `if` used as an expression must have an `else`, and all branches must have the same type.
  `if` used as a statement may omit `else`.
- `break` / `continue` valid only inside loops (checker enforces). No loop values in v1.
- **Block typing.** The type of a block is the type of its last statement. `let`/`var`/
  assignment, `while`/`for`/`loop`, and else-less `if` statements have type `Unit`. For a
  function declared `-> R` (R ≠ Unit) the rule is purely syntactic: the body's last statement
  must be an expression of type R, a `return`, or a `match` whose arms each satisfy this rule
  — otherwise `error: missing return path — function body ends in a statement of type Unit`.
- **Comparison domains.** `==`/`!=` are defined for `Int`, `Float`, `Bool`, `Str` only;
  applying them to any other type (enums, tuples, records, functions, channels) is a compile
  error (E0113). Structural equality on compound values is post-v1. Ordering `< <= > >=` is
  defined for `Int`, `Float`, `Str` only. Both operands must have the same type (no mixed
  Int/Float, per §2.3).

### 2.7 Enums (ADTs), structs, pattern matching

```tantu
enum Color:
    Red
    Green
    Custom(Int, Int, Int)

struct Point:
    x: Float
    y: Float

let p = Point(1.0, 2.0)            # positional construction
let d = p.x + p.y                  # field access

fn describe(c: Color) -> Str:
    match c:
        Red               => "red"
        Custom(r, _, _)   => "custom, r=" + str(r)
        _                 => "other"     # wildcard
```

Patterns (composable/nested): literal (`0`, `-1`, `"x"`, `true`), binding (`name`), wildcard
(`_`), variant (`Some(x)`, `Custom(r, g, b)`), tuple (`(a, b)`). No guards, no or-patterns,
no struct patterns, and no Float literal patterns (checker error — match a Float with a
binding plus comparisons) in v1; guards/or/struct patterns are post-v1 candidates.

- **IDENT patterns are resolved by the checker**: if the name is a variant of the scrutinee's
  enum type, it is a nullary variant pattern; otherwise it is a binding. An IDENT that names
  a variant of a *different* enum is a type error. A binding or wildcard arm makes all later
  arms unreachable (caught by the unreachable-arm check).
- **Constructor namespace**: enum variant names and struct names are injected into the
  module's global value namespace as constructors. Any collision — variant vs variant
  (across enums), variant vs function, struct vs anything — is the standard
  `duplicate definition` error. The prelude reserves `Some`, `None`, `Ok`, `Err`, `Option`,
  `Result`. Qualified variant syntax (`Color.Red`) is post-v1.
- `match` is an expression; all arm bodies must have the same type. Two relaxations: an arm
  ending in `break`, `continue`, or `return` diverges and is exempt from arm-type agreement;
  and a match whose value is discarded (statement position) does not require arm-type
  agreement at all.
- **Exhaustiveness is checked at compile time** for enum and Bool scrutinees:
  `error: match on 'Color' is not exhaustive; missing: Green`. Checking is full and nested
  (implementation reference: Maranget's pattern-matrix usefulness algorithm, which also
  drives unreachable-arm detection). Int/Str matches require a trailing wildcard/binding arm.
- Arm body: inline simple statement or expression after `=>`, or an indented block on
  following lines.

### 2.8 Option, Result, and error propagation — no null, no exceptions

`Option[T]` and `Result[T, E]` are ordinary prelude enums, special only in that `?` knows
them. The sketch below is **not Tantu syntax** (v1 has neither user generics nor `|` variant
lists) — these types are pre-registered in the checker's prelude environment (§3.3), never
parsed from source:

```tantu
enum Option[T]:  Some(T) | None          # conceptual only — not parseable v1 syntax
enum Result[T, E]:  Ok(T) | Err(E)       # conceptual only — not parseable v1 syntax
```

```tantu
fn find_user(id: Int) -> Option[Str]:
    if id == 1: Some("ada") else: None

fn parse_num(s: Str) -> Result[Int, Str]:
    match parse_int(s):                # Option → Result: always an explicit match
        Some(n) => Ok(n)
        None    => Err("not a number: " + s)

fn parse_pair(s: Str) -> Result[(Int, Int), Str]:
    let parts = split(s, ",")
    if len(parts) != 2:
        return Err("expected two fields: " + s)
    let a = parse_num(parts[0])?       # on Err(e): early-returns Err(e) from parse_pair
    let b = parse_num(parts[1])?
    Ok((a, b))
```

Rules for `?` (postfix, binds tighter than any operator):
- On `Result[T, E]` inside a function returning `Result[U, E]` (same `E`): yields `T` or
  early-returns the `Err`.
- On `Option[T]` inside a function returning `Option[U]`: yields `T` or early-returns `None`.
- Mixing (Option inside Result-returning fn) is a type error — convert explicitly with `match`.
- `?` is pure compiler sugar: it desugars to a `match` + `return` during bytecode compilation.

**Panics** are the only other failure mode: div-by-zero, index out of range, `send` on a
closed channel, non-exhaustive match at runtime (should be impossible; assertion), deadlock.
A panic prints a message plus the fiber's stack trace and terminates the **whole program**
(Go-style) with exit code 1. There is no catching panics in v1.

### 2.9 Collections

```tantu
let xs: List[Int] = [1, 2, 3]      # annotation optional when elements infer it
let ys = push(xs, 4)               # NEW list [1,2,3,4]; xs unchanged
let n  = len(xs)
let a  = xs[0]                     # panics if out of range
let b  = get(xs, 99)               # Option[Int] — the safe accessor: None here
let t  = (1, "one")                # tuple, type (Int, Str)
let (num, word) = t                # tuple destructuring in let
```

Tuples have arity ≥ 2 — **there are no 1-tuples** (expression, type, and pattern grammar all
require at least two elements). An annotation on a destructuring `let` must be a tuple type
of matching arity. List concatenation is `concat` only — `+` concatenates strings, never
lists.

`Dict`/`Set` are post-v1. Strings support `len`, `+`, `==`/ordering, `substring(s, i, j)`,
`split(s, sep) -> List[Str]`, `chars(s) -> List[Str]`.

### 2.10 Concurrency syntax

```tantu
let c = chan[Int]()        # unbuffered (rendezvous) channel
let d = chan[Int](16)      # buffered, capacity 16

spawn worker(c, d)         # statement: run any call as a new fiber
spawn fn():                # spawn an anonymous fiber
    send(c, compute())

send(c, 42)                # blocks (parks fiber) until delivered / buffer has room
let v = recv(c)            # Option[Int]: Some(x), or None if channel closed and drained
close(c)                   # further send => panic; further recv drains then returns None
sleep(100)                 # park this fiber for 100 ms
```

The spawn target is a call to a **user-defined** function or lambda, or a bare block-bodied
lambda as above (sugar for spawning a zero-arg call of it). Spawning a builtin or intrinsic
is a compile error (`cannot spawn builtin 'sleep'` — `SPAWN` seeds a fiber from a Closure;
builtins have no frame). The spawned callee must return `Unit`. `sleep(n)` with `n <= 0` is
a pure yield: the fiber moves to the back of the ready queue without touching the clock.
Program exits when `main`'s fiber returns (remaining fibers are dropped, Go-style).

### 2.11 Grammar (EBNF, the parser's contract)

```
module      := { NEWLINE | decl } EOF
decl        := fn_decl | enum_decl | struct_decl | import_decl | stmt
fn_decl     := "fn" IDENT [ type_params ] "(" [ params ] ")" [ "->" type ] ":" block
type_params := "[" IDENT { "," IDENT } "]"        # M9b generics; empty "[]" is a parse error;
                                                  # lambdas cannot declare type params
params      := IDENT ":" type { "," IDENT ":" type }
enum_decl   := "enum" IDENT [ type_params ] ":" NEWLINE INDENT variant { variant } DEDENT
variant     := IDENT [ "(" type { "," type } ")" ] NEWLINE
struct_decl := "struct" IDENT [ type_params ] ":" NEWLINE INDENT field { field } DEDENT
field       := IDENT ":" type NEWLINE
import_decl := "import" IDENT NEWLINE

block       := simple_stmt NEWLINE
             | NEWLINE INDENT stmt { stmt } DEDENT
stmt        := simple_stmt NEWLINE | if_stmt | while_stmt | for_stmt | loop_stmt
             | match_expr_stmt | select_expr_stmt
simple_stmt := let_stmt | var_stmt | assign_stmt | return_stmt | break_stmt
             | continue_stmt | spawn_stmt | expr_stmt
let_stmt    := "let" ( IDENT | "(" IDENT "," IDENT { "," IDENT } ")" ) [ ":" type ] "=" expr
var_stmt    := "var" IDENT [ ":" type ] "=" expr
assign_stmt := IDENT "=" expr
return_stmt := "return" [ expr ]
spawn_stmt  := "spawn" ( postfix_expr | lambda )   # postfix_expr must be a call; target must
                                                   # be a user-defined fn/lambda (§2.10)
while_stmt  := "while" expr ":" block
for_stmt    := "for" IDENT "in" expr ":" block
loop_stmt   := "loop" ":" block
if_stmt     := "if" expr ":" block { "elif" expr ":" block } [ "else" ":" block ]
match_expr_stmt := match_expr
select_expr_stmt := select_expr

expr        := or_expr | if_expr | match_expr | select_expr | lambda
if_expr     := "if" expr ":" expr { "elif" expr ":" expr } "else" ":" expr      # inline form
             | "if" expr ":" block { "elif" expr ":" block } "else" ":" block   # block form
match_expr  := "match" expr ":" NEWLINE INDENT arm { arm } DEDENT
arm         := pattern "=>" arm_body
arm_body    := simple_stmt NEWLINE | NEWLINE INDENT stmt { stmt } DEDENT
select_expr := "select" ":" NEWLINE INDENT select_arm { select_arm } DEDENT
select_arm  := [ IDENT "=" ] "recv" "(" expr ")" "=>" arm_body    # binder "_" discards
             | "send" "(" expr "," expr ")" "=>" arm_body
             | "else" "=>" arm_body                               # must be the single, last arm
lambda      := "fn" "(" [ params ] ")" [ "->" type ] ":"
               ( expr | NEWLINE INDENT stmt { stmt } DEDENT )
or_expr     := and_expr { "or" and_expr }
and_expr    := not_expr { "and" not_expr }
not_expr    := "not" not_expr | cmp_expr
cmp_expr    := add_expr [ ("=="|"!="|"<"|"<="|">"|">=") add_expr ]     # non-associative
add_expr    := mul_expr { ("+"|"-") mul_expr }
mul_expr    := unary { ("*"|"/"|"%") unary }
unary       := "-" unary | postfix_expr
postfix_expr:= primary { "(" [ args ] ")" | "[" expr "]" | "." IDENT | "?" }
primary     := INT | FLOAT | STR | "true" | "false" | IDENT
             | "(" ")" | "(" expr ")" | "(" expr "," expr { "," expr } ")"    # unit/group/tuple (arity >= 2)
             | "[" [ expr { "," expr } ] "]"
             | "chan" "[" type "]" "(" [ expr ] ")"
pattern     := "_" | [ "-" ] INT | STR | "true" | "false" | IDENT
             | IDENT "(" pattern { "," pattern } ")"
             | "(" pattern "," pattern { "," pattern } ")"
type        := IDENT [ "[" type { "," type } "]" ]                    # Int, List[Int], Result[Int,Str]
             | "(" [ type { "," type } ] ")" "->" type                # function type (empty list ⇒ no params)
             | "(" type ")"                                           # parenthesized/grouped type
             | "(" type "," type { "," type } ")"                     # tuple type (arity >= 2)
```

Parser style: **recursive descent** for statements/declarations + the layered expression
grammar above (precedence encoded in the function ladder; no Pratt table needed at this
size). Comparison is non-associative: `a < b < c` is a parse error with a helpful message.

Parsing notes: a statement is terminated by NEWLINE, or implicitly by the DEDENT that closes
a trailing block-form expression (a block `if`/`match`/`select`/lambda as a `let`/`var` RHS
or return value). `assign_stmt` vs `expr_stmt` is LL(2): IDENT followed by `=` (and not `==`) is
assignment. The `chan` primary is a parser special case on the identifier `chan` followed by
`[` (§2.2). In `select_arm`, `recv` and `send` are ordinary IDENT tokens matched by lexeme
(safe because intrinsics cannot be shadowed, §2.2); a leading `IDENT "="` is the recv binder
(LL(2), same trick as assignment), and the binder spelling `_` means "discard the payload".
In **type** position the unit type is spelled `Unit`; `()` is only the unit *literal* (an
expression) and the empty parameter list of a function type. A bare `()` in type position
that is not followed by `->` is a parse error that points at `Unit`.

---

## 3. Type System

### 3.1 What is checked, and where

Type checking is a **dedicated pass** (`tantu/checker.py`) that runs on the AST **after
parsing and before bytecode compilation**. The compiler stage *asserts* it only ever sees a
checked AST. Nothing about types survives into the VM — the VM trusts the checker
(erasure), which is what lets runtime values be plain Python objects.

Statically checked (compile-time errors, all collected and reported together, not
first-error-only):
1. Every expression has exactly one type; operator/call/argument type agreement.
2. Name resolution: unknown names, use-before-def at module level, duplicate definitions,
   assignment to `let`, capture of `var`, `break` outside loop, missing `return` paths.
3. `if`/`match` branch-type agreement; condition is `Bool` (no truthiness — `if xs:` is an
   error, write `if len(xs) > 0:`).
4. Match exhaustiveness (enums, Bool) and unreachable-arm detection (arm after wildcard).
5. `?` operator rules (§2.8); function return-type conformance.
6. Channel element types; `spawn` target is a user-defined fn/lambda returning `Unit`.

### 3.2 Inference vs annotation — the boundary

- **Required**: function parameter types, function return types (unless Unit), struct
  fields, enum variant payloads.
- **Inferred**: everything inside function bodies, via bottom-up synthesis plus a
  *bidirectional-lite* expected-type that flows downward from `let x: T =`, call argument
  positions, and `return`. This is what makes `let xs: List[Int] = []` and
  `let m: Option[Int] = None` work, while bare `let xs = []` is an error:
  `cannot infer element type of []; add an annotation`.
- **No general unification/HM.** One tiny unifier exists solely to instantiate the
  signatures of *builtin* generics at call sites (`push: (List[T], T) -> List[T]`,
  `get: (List[T], Int) -> Option[T]`, `send: (Chan[T], T) -> Unit`, …). It handles type
  variables in one signature at one call site — ~60 lines, no constraint solving across
  expressions. A builtin's registry entry is a *list* of signatures tried in order; the
  first that unifies wins (error if none). `len` is the only multi-entry builtin in v1.

### 3.3 Type representation (`tantu/ttypes.py`)

```python
# frozen dataclasses; structural equality is type equality
class Type: ...
@dataclass(frozen=True) class Prim(Type):    name: str            # "Int","Float","Bool","Str","Unit"
@dataclass(frozen=True) class ListT(Type):   elem: Type
@dataclass(frozen=True) class TupleT(Type):  items: tuple[Type, ...]
@dataclass(frozen=True) class FnT(Type):     params: tuple[Type, ...]; ret: Type
@dataclass(frozen=True) class ChanT(Type):   elem: Type
@dataclass(frozen=True) class EnumT(Type):   name: str; args: tuple[Type, ...]   # Option[Int] => EnumT("Option",(Int,))
@dataclass(frozen=True) class StructT(Type): name: str
@dataclass(frozen=True) class TypeVar(Type): name: str            # ONLY inside builtin signatures
INT, FLOAT, BOOL, STR, UNIT = Prim("Int"), ...
```

`Option`/`Result` are `EnumT`s pre-registered in the prelude environment with their variant
tables (`Some/1, None/0`, `Ok/1, Err/1` — name/arity) — the checker and compiler treat them
identically to user enums; only the `?` desugar and `recv` signature name them specially.
Variant **tags are declaration-order indices** (`Some=0, None=1`; `Ok=0, Err=1`); the prelude
variant descriptors live in the single registry (`prelude.py`, §6.3) imported by both the
compiler and the VM, so VM-constructed `recv` results and compiled `TEST_VARIANT` tags
cannot disagree.

### 3.4 Generics policy

v1 user code is **monomorphic**: users cannot declare `fn id[T](x: T) -> T` or generic
enums/structs. Builtin containers are generic via the mini-unifier above. User-defined
generics are Milestone 9 (stretch) and the design leaves room: `EnumT.args` already carries
type arguments, so adding declaration-site type parameters later does not change the type
representation.

*[v1.2: superseded — M9b shipped declaration-site generics for fn/enum/struct (`fn f[T, U]`,
`enum E[T]:`, `struct S[A, B]:`, grammar in §2.11). Generics are ERASED, not monomorphized:
checked polymorphically, compiled once; type arguments are always inferred (explicit type
args are post-v1); a generic fn is call-position-only, never a first-class value (E0136).
Full rulings in DECISIONS.md, M9b rows.]*

### 3.5 Checker output contract

AST nodes are plain (non-frozen) dataclasses. The checker **annotates in place** and
returns the same module object:
- every expression node's `ty: Type | None` field is filled in;
- every `Name` node gets `res: Resolution` — `Local(slot:int)` | `Global(index:int)` |
  `Capture(index:int)` | `Builtin(name:str)`;
- every function node gets `n_locals: int` and its capture list;
- every `match` gets a `MatchPlan` (ordered arms with resolved variant tags).

One AST, one annotation pass, no parallel "TypedAST" class hierarchy — a deliberate
anti-mismatch decision (a second hierarchy is where old attempts would have diverged).
The compiler contains `assert node.ty is not None` guards so an unchecked AST fails loudly.

---

## 4. Concurrency Model

### 4.1 Decision: CSP — fibers + channels (Go model). Not actors.

Rationale: (a) channels give exactly two blocking primitives (`send`, `recv`) — the entire
scheduler/VM interaction surface is two opcodes plus `sleep`, whereas actors need per-process
mailboxes, selective receive, and PID plumbing; (b) channel element types slot directly into
the static type system (`Chan[T]`), while typed actor protocols are a research project;
(c) with `var`-capture banned (§2.4) fibers share no mutable state, so CSP here is
race-free by construction, which is the honest version of "Erlang-style safety" at this
scale. `select` over multiple channels shipped in M9a and is specified in §4.6.

### 4.2 The GIL question, answered

The VM runs on **one OS thread, period**. Fibers are cooperative green threads multiplexed
by the VM's own scheduler — this provides *concurrency* (interleaved progress, blocking
without stalling the world), not *parallelism*. Under the locked "correctness and clarity
over raw speed" priority this is the right trade: no GIL fights, no `threading`, no
`asyncio`, and — critically — **deterministic scheduling**, which makes concurrent programs
unit-testable with golden outputs. (Parallelism via multiple VM processes is out of scope.)

### 4.3 Scheduler design

- **Fiber** = independent execution context: its own frame stack and value stack (§5.4).
  States: `READY`, `RUNNING`, `PARKED` (in a channel/timer wait queue), `DONE`.
- **Scheduler** owns: `ready: deque[Fiber]` (round-robin), `timers: heapq[(wake_at, seq, fiber)]`,
  and counters of live fibers.
- **Preemption budget**: the interpreter runs the current fiber for at most
  `BUDGET = 1000` instructions, then rotates it to the back of `ready`. This prevents a
  `loop:` fiber from starving others, with zero user-visible yield points. Budget is a VM
  constructor arg so tests can set it to 1 for maximal interleaving stress. Every entry into
  the dispatch loop gets the full budget; a fiber that parks forfeits the remainder.
- **Blocking**: `CHAN_SEND` on a full/unbuffered channel and `CHAN_RECV` on an empty one
  park the current fiber into that channel's wait deque; `SLEEP` parks into the timer heap.
  Parking = the opcode handler stores the fiber in the wait structure and returns a `PARK`
  signal to the dispatch loop, which then picks the next ready fiber.
- **Unparking**: woken fibers are appended to the **back** of `ready`, in the order they are
  woken; waking never preempts the running fiber. Timers pop in `(wake_at, seq)` order and
  are appended in that order — fully deterministic given the injected clock.
- **Idle**: if `ready` is empty but timers exist, the scheduler calls the injected
  `sleep_fn(until_next_timer)` (default `time.sleep`) then wakes due fibers. Tests inject a
  fake clock plus a `sleep_fn` that advances it, so timer tests are instant and deterministic.
- **Deadlock detection for free**: `ready` empty + no timers + parked fibers exist ⇒
  `panic: deadlock — every fiber is blocked` (prints each parked fiber's stack trace). This
  includes the case where only the main fiber is parked and every other fiber is DONE —
  DONE fibers are not work.
- **Exit**: when the main fiber's outermost frame returns, the program exits (code 0);
  remaining fibers are discarded. A panic in *any* fiber exits the program (code 1).

### 4.4 Channel semantics (`tantu/values.py: Channel`)

```
Channel: buf: deque, cap: int (0 = unbuffered), closed: bool,
         send_wait: deque[(fiber, value)], recv_wait: deque[fiber]
send(v): closed → panic
         a receiver is parked → hand v directly to it, mark it READY
         len(buf) < cap       → append
         else                 → park self with v
recv():  buf nonempty → pop; if a parked sender exists, move its value in, ready it
         a sender is parked (cap 0) → take its value, ready it
         closed & drained → return None-variant immediately
         else → park self
close(): already closed → panic ("close of closed channel", Go-style)
         a sender is parked → immediate panic "send on closed channel", reported with that
                              sender fiber's stack trace
         else: closed=True; ready every parked receiver (each will see None)
```

**Parking protocol** (the resumption contract): a parking opcode fully consumes its operands
and pre-pushes its result *before* parking — `()` for `CHAN_SEND`/`SLEEP`. For `CHAN_RECV`
the counterparty deposits the result: the sender (or `close`) constructs the `Some(v)`/`None`
variant and pushes it onto the parked fiber's value stack before marking it READY. `ip` has
already advanced when the handler runs, is never rewound, and instructions are never
re-executed.

`recv` returning `Option[T]` (not a bare value) is the design linchpin: channel shutdown is
an ordinary pattern-match, no sentinel values, no exceptions — see the showcase program.

### 4.5 Concurrency opcodes & structures (summary; full ISA in §5)

`SPAWN argc`, `CHAN_NEW` (cap on stack), `CHAN_SEND`, `CHAN_RECV`, `CHAN_CLOSE`, `SLEEP`,
plus the M9a pair `SELECT`/`SELECT_DISPATCH` — eight opcodes total. VM structures: `Fiber`,
`Scheduler`, `Channel` as above. No locks — the whole VM is single-threaded, so scheduler
data structures need no synchronization.

### 4.6 `select` — waiting on multiple channels (shipped, M9a)

`select` is an **expression** that waits on several channel operations at once and runs the
arm of the first one that can proceed:

```tantu
let msg: Option[Int] = select:                    # annotation required — `None` needs the expected type
    v = recv(data)     => v                       # v: Option[Int] — recv's normal contract
    send(out, answer)  => Some(0)                 # fires when out can accept answer
    else               => None                    # optional: fires if nothing is ready
```

Syntax (§2.11 `select_expr`): a match-style indented suite of arms. Arm heads are
`[binder =] recv(chan)`, `send(chan, value)`, or `else`; a binder of `_` discards the
payload. Arm bodies follow match's exact arm rules (inline simple statement or indented
block; expression-valued).

**Typing** (checker): a recv binder has type `Option[T]` for a `Chan[T]` head — recv's
single contract (§4.4) is unchanged inside select. A send arm's value must have the
channel's element type. Arm bodies obey match's arm-value rules: in expression position all
arms must have the same type (divergent arms ending in `break`/`continue`/`return` are
exempt); in statement position the value is discarded and arm types need not agree. `else`
must be the **single, last** arm (E0135).

**Evaluation & semantics** (compiler/VM, `SELECT` + `SELECT_DISPATCH`):
1. All arm operands are evaluated **up front, in source order** — each recv arm's channel
   expression, each send arm's channel and value expressions (`else` contributes none). Then
   a single `SELECT` executes.
2. **Phase 1 — poll, source order.** Arms are polled in source order and the *first ready*
   arm fires immediately, with exactly the ready-path effects of `CHAN_RECV`/`CHAN_SEND`
   (§4.4): a recv arm is ready if the channel has buffered values, a parked sender, or is
   **closed** (a closed channel makes recv arms fire with `None` — they are never skipped);
   a send arm is ready if a receiver is parked or the buffer has room. A send arm whose
   channel is closed **panics R003** ("send on closed channel") when it is the first-ready
   arm. No randomization (Go-style shuffling was rejected): polling is deterministic, and
   starvation of an always-shadowed later arm is the accepted price.
3. **No ready arm + `else`**: the `else` arm fires. A select with an `else` arm never parks.
4. **No ready arm, no `else` — phase 2: park.** The fiber registers a waiter on *every*
   channel arm and parks. The first counterparty event (a send, a recv, or a `close`)
   resolves the whole select atomically: a shared `won` flag flips, and the registrations
   left on the other channels become tombstones that the channel wait-queues skim over
   later (O(1) cancellation; a `close` wakes a parked recv arm with `None`, and wakes a
   parked send arm with a pending R003 panic raised on resume). The two-phase
   poll-then-register protocol also means a same-channel `send` + `recv` pair inside one
   select can never rendezvous with itself.
5. **Dispatch.** The counterparty (or the immediate poll) deposits one value — the pair
   `(case_index, payload)` — preserving the §4.4 one-deposit parking protocol; the
   instruction after `SELECT` is always `SELECT_DISPATCH`, which pops the pair, pushes the
   payload (`Some(v)`/`None` for a recv arm; discarded Unit for send/else), and jumps to the
   arm's body. `ip` is never rewound.

v1 select has **no timer arm**: a timeout is written by spawning a sleeper fiber that sends
on a timeout channel, then selecting over data + timeout channels.

---

## 5. Bytecode & VM Architecture

### 5.1 Value representation (erased types — checker guarantees soundness)

| Tantu value | Python runtime representation |
|---|---|
| Int / Float / Bool / Str | native `int` / `float` / `bool` / `str` |
| Unit | `None` |
| List | Python `list` — **immutable by convention**: no builtin ever mutates one; `push`/`concat` return new lists |
| Tuple | Python `tuple` |
| enum value | `Variant(tag: int, enum_name: str, variant_name: str, fields: tuple)` |
| struct value | `Record(struct_name: str, fields: tuple)` |
| function | `Closure(code: Code, captures: tuple)` or `Builtin(name, fn, arity)` |
| channel | `Channel` (§4.4) |

Using native Python values for primitives is deliberate: the type checker already proved
operations well-typed, so tagging every int would be pure overhead and noise. List and Tuple
must be *distinct* Python types because types are erased and `render()` (Appendix B) prints
them differently (`[1, 2]` vs `(1, 2)`). `Variant`/`Record`/`Closure` are
`@dataclass(frozen=True, slots=True)`.

### 5.2 The `Code` object (`tantu/bytecode.py`) — the compiler↔VM contract

```python
@dataclass
class Code:
    name: str                      # "main", "<module>", "worker"
    arity: int
    n_locals: int                  # params occupy slots 0..arity-1
    n_globals: int                 # size of the module's global table (VM allocates from this)
    instrs: list[Instr]            # Instr = (op: Op, arg: int | tuple[int, ...] | None) frozen+slots  [v1.1a correction — arg2 removed, see DECISIONS.md]
    consts: list[object]           # ints, floats, strs, Code objects, variant descriptors
    spans: list[Span]              # parallel to instrs — powers stack traces
    def dis(self) -> str: ...      # human-readable disassembly (used by golden tests)
class Op(enum.IntEnum): ...
```

Jump arguments are **absolute instruction indices** (not byte offsets — instrs are objects,
not bytes; "bytecode" names the shape, not the encoding). The compiler uses a
`emit_jump()/patch()` pair for forward references.

### 5.3 Instruction set (complete v1 ISA)

Stack effect notation: `before → after` (top on the right).

| Op | arg | Stack effect | Semantics |
|---|---|---|---|
| `LOAD_CONST k` | const idx | `→ v` | push `consts[k]` |
| `LOAD_LOCAL i` | slot | `→ v` | push `frame.locals[i]` |
| `STORE_LOCAL i` | slot | `v →` | `frame.locals[i] = v` |
| `LOAD_GLOBAL i` | global idx | `→ v` | push `vm.globals[i]` |
| `STORE_GLOBAL i` | global idx | `v →` | module init / REPL only |
| `LOAD_CAPTURE i` | capture idx | `→ v` | push `frame.closure.captures[i]` |
| `POP` | — | `v →` | discard |
| `DUP` | — | `v → v v` | duplicate top |
| `ADD SUB MUL DIV MOD` | — | `a b → r` | arithmetic; `+` also Str concat (never lists — §2.9); Int DIV/MOD floor; zero divisor ⇒ panic |
| `NEG` | — | `a → -a` | |
| `NOT` | — | `a → !a` | |
| `EQ NE LT LE GT GE` | — | `a b → Bool` | primitives only (Int/Float/Bool/Str); the checker rejects EQ/NE on Variant/Record/tuple (E0113), so those cases are unreachable — structural equality is post-v1 |
| `JUMP t` | instr idx | `→` | unconditional |
| `JUMP_IF_FALSE t` | instr idx | `Bool →` | pops |
| `JUMP_IF_TRUE t` | instr idx | `Bool →` | pops (compiles `and`/`or` with a DUP/POP pattern) |
| `MAKE_CLOSURE k n` | arg=(k, n) | `c1..cn → clo` | capture n values (k=Code const idx; no k*256+n packing — see DECISIONS.md) |
| `CALL n` | argc | `f a1..an → r` | Closure: push new Frame. Builtin: call Python fn |
| `RETURN` | — | `v →` (frame pops) | pop frame; push v onto caller's stack; if last frame ⇒ fiber DONE |
| `BUILD_LIST n` | count | `v1..vn → list` | as Python list |
| `BUILD_TUPLE n` | count | `v1..vn → tup` | |
| `INDEX` | — | `xs i → v` | bounds-check ⇒ panic |
| `GET_FIELD i` | field idx | `obj → v` | Record field (`p.x`; checker resolves name→index) or tuple element (destructuring); never Variant — payloads use UNPACK_VARIANT |
| `MAKE_VARIANT k` | k=descriptor const | `p1..pn → variant` | descriptor = (enum, variant, tag, arity); n read from descriptor |
| `MAKE_RECORD k` | k=descriptor const | `f1..fn → record` | descriptor = (struct, field names); n read from descriptor |
| `TEST_VARIANT t` | tag | `v → v Bool` | peek: does TOS have tag t? (non-destructive) |
| `UNPACK_VARIANT n` | arity | `variant → p1..pn` | push payload fields |
| `MATCH_FAIL` | — | — | panic "non-exhaustive match" (unreachable safety net) |
| `SPAWN n` | argc | `clo a1..an →` | new Fiber, seed its frame, append to ready |
| `CHAN_NEW` | — | `cap → chan` | |
| `CHAN_SEND` | — | `chan v → ()` | may PARK current fiber (§4.4) |
| `CHAN_RECV` | — | `chan → Option` | may PARK; yields `Some(v)`/`None` Variant |
| `CHAN_CLOSE` | — | `chan → ()` | |
| `SLEEP` | — | `ms → ()` | ms <= 0: yield to back of ready; else PARK on timer heap |
| `SELECT` | arg=(has_else, kind…) | `ops… → (idx, payload)` | kind: 0=recv (pops chan), 1=send (pops chan, value), operands in source order; poll arms in source order, first ready fires; else (case index = channel-arm count) if none; otherwise PARK on all arms (§4.6) |
| `SELECT_DISPATCH` | arg=(target…) | `(idx, payload) → payload` | always follows SELECT; pops the pair, pushes payload, computed-jump to `targets[idx]` (absolute instr indices) |
| `PRINT` | — | `v → ()` | writes `render(v) + "\n"` to VM's stdout (a dedicated op keeps M1 free of the call machinery) |
| `HALT` | — | — | end of `<module>` code ⇒ main fiber DONE, exit 0 |

Ops with stack effect `→ ()` push `None` (Unit); in statement position the compiler emits a
following `POP`.

Pattern-match compilation strategy (naive and correct, optimize never): scrutinee is stored
in a hidden local; each arm: load it, run pattern tests (`TEST_VARIANT`/`DUP;LOAD_CONST;EQ`
for literals, recursing into payloads via `UNPACK_VARIANT` into more hidden locals),
`JUMP_IF_FALSE` to next arm, else bind pattern variables to locals and run the body, `JUMP`
to end. `MATCH_FAIL` after the last arm. Tuple patterns compile to `GET_FIELD i` per element
against a hidden local; variant payloads use `UNPACK_VARIANT`.

Tuple destructuring (`let (a, b) = t`) uses the same shape: store `t` in a hidden local, then
`LOAD_LOCAL h; GET_FIELD 0; STORE_LOCAL a; LOAD_LOCAL h; GET_FIELD 1; STORE_LOCAL b`.

`?` desugar (no new opcodes; the Result/Option value is on TOS): `TEST_VARIANT tag(Ok|Some);
JUMP_IF_TRUE ok; RETURN` — the `Err`/`None` variant is returned *unchanged* (legal because
`E` matches per §2.8) — `ok: UNPACK_VARIANT 1` leaves the payload on the stack.

`for x in xs` lowering (no iterator opcodes): hidden locals `h_xs = <expr>`, `h_i = 0`,
`h_n = len(h_xs)` (ordinary builtin call); loop head: `LOAD h_i; LOAD h_n; LT;
JUMP_IF_FALSE end; LOAD h_xs; LOAD h_i; INDEX; STORE_LOCAL item; <body>; LOAD h_i;
LOAD_CONST 1; ADD; STORE_LOCAL h_i; JUMP head`. `continue` jumps to the increment, `break`
to `end`. Safe because lists are immutable; `item` behaves as a fresh `let` per iteration
(capture is by value).

### 5.4 Frames, fibers, and the dispatch loop (`tantu/vm.py`)

```python
class Frame:  code: Code; ip: int; locals: list; closure: Closure|None; stack_base: int
class Fiber:  fid: int; frames: list[Frame]; stack: list; state: State
class VM:
    globals: list                  # module global table (Code.n_globals slots), shared by all fibers
    def __init__(self, budget=1000, stdout=sys.stdout, clock=time.monotonic, sleep_fn=time.sleep): ...
    def run(self, module_code: Code) -> int: ...     # exit status
```

One value `stack` per fiber, shared across its frames via `stack_base` (CPython-style).
Dispatch loop shape — **scheduling is woven into fetch-decode-execute**:

```python
while not main_fiber_done:
    fiber = scheduler.next_ready()            # ready → run it; else timers → sleep_fn until
                                              # nearest, wake due; else ⇒ panic: deadlock
                                              # (parked fibers necessarily exist)
    signal = self.execute(fiber, self.budget) # inner loop below
    # signal ∈ {BUDGET_EXHAUSTED → requeue, PARKED → nothing (a channel/timer holds it),
    #           DONE → if fiber is main: return}

def execute(self, fiber, budget):
    frame = fiber.frames[-1]
    for _ in range(budget):
        instr = frame.code.instrs[frame.ip]; frame.ip += 1
        ...match/if dispatch on instr.op...   # CALL/RETURN switch `frame`; CHAN_* may return PARKED
    return BUDGET_EXHAUSTED
```

Dispatch is a plain `if/elif` chain ordered by expected frequency (or `match` on IntEnum) —
measured-fast-enough for Python, trivially debuggable. Panic = a Python `TantuPanic`
exception carrying the message; caught once in `run()`, rendered with the fiber's stack
trace built from `frames[*].code.name` + `spans[ip - 1]` — ip is post-incremented, so ip-1
is the instruction in flight: the CALL for caller frames, the faulting/parking op for the
top frame.

### 5.5 Calling convention

Caller pushes callee then args left-to-right; `CALL n` **pops** the callee and the n args
(args move into `frame.locals[0..n-1]`), validates arity (assertion — checker already proved
it), creates `Frame(locals=[a1..an] + [None]*extra)`, and sets
`stack_base = len(fiber.stack)` after the pops. `RETURN` pops the return value, truncates
the fiber stack to `stack_base` (assert it already equals it — a cheap VM invariant), pops
the frame, pushes the return value onto the caller's stack. Builtins are called synchronously
in Python (they never block; all blocking ops are opcodes).

**Entry point**: for `tantu run`, the compiler appends a `<module>` epilogue after top-level
code: `LOAD_GLOBAL <main>; CALL 0; POP; HALT` — checker error if no `main() -> Unit` exists
in run mode (pure top-level scripts are fine in M1–M2 before functions land; REPL and `dis`
emit just `HALT`). Spawned fibers end via `RETURN` of their seeded frame; only `<module>`
executes `HALT`.

---

## 6. Compiler Pipeline & Module Architecture

The #1 killer of the old attempts was interface drift between stages. Therefore: every
stage is one module with **one public function with an exact signature**, consuming only
the previous stage's output type. Cross-stage types live in exactly one home each.

### 6.1 Package layout

```
tantu/
  __init__.py
  __main__.py        # python -m tantu  → cli.main()
  cli.py             # arg parsing: run | check | dis | repl(default); wires pipeline; renders errors
  errors.py          # Span; TantuError(stage, msg, span, hint); error-code registry (App. C); render_error(src, err) -> str
  tokens.py          # TokenType(enum), Token — NO logic
  lexer.py           # tokenize()
  ast_nodes.py       # all AST dataclasses (NOT 'ast' — never shadow a stdlib name; attempt #1 had AST.py beside 'import ast')
  ttypes.py          # Type dataclasses (§3.3) + prelude type environment (NOT 'types' — stdlib shadow; 'dataclasses' itself imports 'types')
  parser.py          # parse()
  checker.py         # check()  (name resolution + types + exhaustiveness, one pass)
  bytecode.py        # Op, Instr, Code, dis()
  compiler.py        # compile_module()
  values.py          # Variant, Record, Closure, Builtin, Channel, render(value)->str
  prelude.py         # builtin table (name -> Builtin + signatures) AND prelude enum variant descriptors; shared by checker & VM (NOT 'builtins' — stdlib shadow)
  vm.py              # VM, Fiber, Frame, Scheduler
  repl.py            # REPL (§6.4)
tests/               # §8
```

The remaining module names audit clean against the 3.12 stdlib (`parser`/`compiler` were
removed long ago; `tokens` ≠ stdlib `token`). The repo root carries `pyproject.toml` with
`requires-python = ">=3.12"` and a pinned pytest; `cli.main()` asserts 3.12+ with a friendly
message. `--version` ships from M1 (it is three lines).

### 6.2 Stage contracts (exact, non-negotiable)

| # | Stage | Function | Input | Output | Errors raised |
|---|---|---|---|---|---|
| 1 | Lexer | `tokenize(src: str, file: str) -> list[Token]` | source text | tokens, ending `[..., NEWLINE?, DEDENT*, EOF]` | `LexError` |
| 2 | Parser | `parse(tokens: list[Token]) -> Module` | token list | AST `Module(decls, span)` | `ParseError` |
| 3 | Checker | `check(m: Module, env: Prelude) -> Module` | AST | **same** AST, fully annotated (§3.5) | `CheckErrors([TantuError])` — collected |
| 4 | Compiler | `compile_module(m: Module) -> Code` | checked AST | `Code` for `<module>` (nested `Code`s in consts) | none (asserts only) |
| 5 | VM | `VM(...).run(code: Code) -> int` | `Code` | exit status; side effects on injected stdout | `TantuPanic` handled internally |

`Token = (type: TokenType, lexeme: str, value: int|float|str|None, span: Span)` — frozen.
AST nodes: mutable dataclasses, every node has `span: Span`; expressions add
`ty: Type|None = None`. All errors subclass `TantuError` and are rendered by ONE function
(`errors.render_error`) that prints the offending source line with a caret — every stage,
same format, from Milestone 1 onward. Process exit codes: **0** success, **1** panic,
**2** compile-time error (lex/parse/check), **130** interrupted (Ctrl-C during
`run`/`check`/`dis`; the REPL instead prints `KeyboardInterrupt` and continues),
**141** broken output pipe (stdout or stderr) — the consumer went away (e.g. `tantu run x | head`; 128 + SIGPIPE, silent). Rendered errors print paths relative to the CWD
so golden `.err` files are machine-independent.

### 6.3 Data-flow invariants (enforced by tests)

- The lexer is the only module that sees raw text; the parser never touches `src`.
- The parser produces no types; the checker produces no instructions; the compiler reads
  `ty`/`res` annotations but never re-infers; the VM imports nothing from
  `parser/checker/compiler` (only `bytecode`, `values`, `errors`).
- `prelude.py` is the single registry read by both checker (signatures) and VM
  (implementations) — one table for builtins *and* prelude enum variant descriptors
  (Option/Result tags, §3.3), so they cannot disagree.

### 6.4 REPL contract

Prompts `>>> ` and `... `. The continuation decision is made **once, on the first line**: a
(comment-stripped) line ending in `:`, or a line with unclosed brackets, enters continuation
mode. Once in continuation mode, lines are read until a **blank line** (or EOF) submits the
whole buffer — blank-line termination applies to bracket continuations exactly as to block
headers. Each input runs through the full pipeline against a **persistent** global
environment: one name→index table plus the growing `vm.globals` list and the type
environment, threaded into `check()`/`compile_module()` for every input. New top-level names
append (indices are never renumbered); re-`let` of an existing name rebinds the same index —
the one REPL-only relaxation of §2.4. A non-Unit expression result is rendered exactly as
`print` renders it (top-level rendering, Appendix B — a bare string echoes unquoted) and echoed. Errors render normally and do not end the
session. Ctrl-D exits 0. `spawn` at the prompt runs the scheduler to quiescence before
re-prompting. `repl.py` takes `(stdin, stdout)` so `test_repl.py` drives it with StringIO.

---

## 7. Phased Roadmap

Every milestone ends **runnable and green under pytest**. Estimates assume a solo dev with
prior context, in focused evenings/weekends (1 wk-end ≈ 2 days).

| M | Name | Builds | Acceptance test | Effort |
|---|------|--------|-----------------|--------|
| **M1** | Arithmetic REPL, end-to-end | Full skeleton of ALL modules in §6.1 (checker is pass-through returning Int for everything). Lexer: ints, floats, IDENT, `+-*/%(),`, NEWLINE, EOF (INDENT machinery present but only exercised trivially). Parser: expression ladder incl. call postfix. Compiler: consts+arith; `print(expr)` recognized as intrinsic and lowered to `PRINT` (no `CALL` op until M3); `HALT`. VM: single fiber, one implicit `<module>` frame, no CALL/RETURN. REPL + `run` + `dis` CLI, `--version`. Error rendering with carets. | `echo 'print(1 + 2 * 3)' \| python -m tantu run -` prints `7`; `print(10 / 0)` panics with caret’d one-frame trace; 6+ golden run tests, 3+ golden error tests pass. | 1 wk-end |
| **M2** | Bindings, Bool/Str, if/while, real type checking | `let/var`, assignment rules, `Bool/Str/Unit` types, comparisons, `and/or/not`, if (stmt+expr), `elif`, `while/break/continue`, blocks ⇒ **INDENT/DEDENT fully live**, real checker (env stack, inference in bodies; `print`/`str` typed ad hoc — accept any argument — until M4's mini-unifier), `LOAD/STORE_LOCAL/GLOBAL`, jumps, `tantu check` CLI mode. | FizzBuzz runs (pinned as `tests/golden/run/fizzbuzz.tn` — per-branch prints, no `str()`, which would need M3's CALL); `let x = 1` then `x = 2` yields exact error `cannot assign to immutable binding 'x'`; `if 1:` rejected (`condition must be Bool`); type errors are collected (2 errors in one file → both reported). | 1–2 wk-ends |
| **M3** | Functions & closures | `fn` decls, calls, recursion, anonymous `fn`, function types, two-pass module checking, `MAKE_CLOSURE/CALL/RETURN`, frames, capture-by-value, `var`-capture error, stack traces through frames. | Recursive `fib(20)` = 6765; mutual recursion `is_even/is_odd`; an adder factory works (`make_adder(3)(4)` = 7 — a returned closure capturing `let n`); a counter-factory over a captured `var` produces the specified compile error (mutable captured state is impossible by design, §2.4); wrong-arity call is a compile error. | 1–2 wk-ends |
| **M4** | Enums, Option/Result, `match` | `enum` decls, variant construction, prelude Option/Result, builtin-generics mini-unifier (§3.2 — needed *here* by `Some`/`Ok`/`Err`, not M5), `match` with nested patterns, **exhaustiveness + unreachable-arm checking** (full nested checking; reference: Maranget's pattern-matrix algorithm — the hardest single algorithm in the project), `MAKE/TEST/UNPACK_VARIANT`, structural equality, `render()` prints `Some(3)`-style. | Showcase `Shape/area` program (minus fibers) runs; omitting `Green` from a Color match yields `missing: Green`; nested `Some((a, b))` match works; golden dis-test locks match compilation shape. | 2–3 wk-ends |
| **M5** | Collections, structs, `for`, stdlib-lite | `List[T]` literals/`INDEX`/builtins (`len,push,get,range,concat`), tuples + destructuring `let`, `struct` + field access, `for x in xs`, Str builtins (`split,substring,chars,parse_int`) — the mini-unifier already landed in M4. | Word-frequency program (pinned as `examples/wordfreq.tn`; assoc-list fold, counts printed in first-occurrence order) matches golden output; `xs[99]` panics, `get(xs,99)` is `None`; a `for` loop accumulating `range(0,101)` into a `var` prints 5050. | 1–2 wk-ends |
| **M6** | Error handling: `?` and panics | `?` on Result and Option (desugar in compiler), checker rules from §2.8, panic machinery finalized (message + full fiber stack trace + exit 1), `render_error` polish. | A 3-level `Result` pipeline propagates an `Err` from the bottom to `main`; using `?` on `Result` in an `Option` fn is a compile error; panic trace shows 3 frames with line numbers. | 1 wk-end |
| **M7** | Concurrency: fibers + channels | `Fiber/Scheduler/Channel`, budget preemption, `spawn`, `chan/send/recv/close/sleep`, timer heap, deadlock detection, `ChanT` checking, spawn-must-return-Unit rule. | Showcase program (§1) prints its two lines **in order**; unbuffered ping-pong (2 fibers, 10 round trips) golden-matches with `budget=1`; recv-on-never-sent panics `deadlock`; fan-in works (3 producers each send N values on a shared channel then signal on a `done` channel; main recvs `done` 3 times, closes the shared channel, drains until `None`); all prior goldens still pass. | 2 wk-ends |
| **M8** | Modules + toolchain polish | `import name` (sibling `name.tn`), qualified `name.fn` (checker disambiguates from field access via `Name` resolution), module cache + cycle error, module-level `let` init order, **two-level global addressing** (per-module global tables sized by `Code.n_globals` — touches compiler *and* VM; the single-table design of M1–M7 is the deliberate simplification this milestone lifts), `tantu dis` on any function, README + 10-program `examples/`. | Two-file program where `main.tn` imports `shapes.tn` runs; import cycle reports both files; every example in `examples/` runs green in CI (the golden suite). | 2–3 wk-ends |
| **M9** *(stretch)* | User generics OR `select` | Pick ONE: declaration-site generics for fns/enums, or `select` over channels. | Generic `map: (List[A],(A)->B)->List[B]` in Tantu itself, or a timeout-with-select example. | 2 wk-ends |

Total to portfolio-complete (M1–M8): **~11–16 weekends (≈22–32 focused days)**, at the
header's "1 wk-end ≈ 2 days". Ship after M8; M9 only if appetite remains.

---

## 8. Testing Strategy

**Tooling**: pytest only. Every milestone adds tests; no milestone merges red.

### 8.1 Layers

1. **Unit, per stage** (fast, thousands-per-second):
   - `test_lexer.py`: token-stream assertions incl. the INDENT/DEDENT table cases (nested
     blocks, blank lines, comments, bracket continuation, tab rejection, EOF dedents).
   - `test_parser.py`: `parse(tokens).pretty()` (a compact s-expression dump on every AST
     node, e.g. `(if (> x 0) (block ...) ...)`) compared to expected strings — readable
     diffs, no brittle object graphs.
   - `test_checker.py`: positive typing cases assert `node.ty`; negative cases assert
     error *codes + spans* (each TantuError has a stable code like `E0102-assign-immutable`;
     registry in Appendix C).
   - `test_compiler.py`: `compile_module(...).dis()` golden strings for canonical snippets
     (locks the ISA and match-compilation shape).
   - `test_vm.py` / `test_concurrency.py`: hand-assembled `Code` objects run on a VM with
     `stdout=StringIO`, including budget=1 interleaving tests and channel/timer unit tests.
2. **Golden end-to-end** (the backbone; starts in M1):
   ```
   tests/golden/run/hello.tn      + hello.out        # expected stdout, exit 0
   tests/golden/run/fib.tn        + fib.out
   tests/golden/error/assign_let.tn + assign_let.err # expected rendered compile error
   tests/golden/panic/div_zero.tn + div_zero.out + exit code 1
   ```
   `conftest.py` auto-parametrizes over these directories; adding a test = adding two
   files. `--update-goldens` flag regenerates `.out` files after intentional changes.
   A golden file's **first line** may be `# tantu-test: key=value[, key=value]`; the only
   recognized key in v1 is `budget` (int); unknown keys are an error. The runner executes
   files in-process (the §6.2 pipeline functions + `VM(stdout=StringIO, budget=...)`),
   never via subprocess.
3. **Determinism rule**: the scheduler is deterministic (fixed round-robin + budget), so
   concurrent programs are golden-testable. Concurrency goldens run at both default budget
   and `budget=1` (via a `# tantu-test: budget=1` header comment the runner reads). `sleep`
   tests assert ordering only, never wall-clock durations.
4. **Pipeline smoke**: one test imports every module and runs `hello.tn` through the five
   public functions asserting the §6.2 types at each boundary — the anti-interface-drift tripwire.
5. **Seeded fuzz** (`test_fuzz.py`, zero new deps): (a) the lexer over random byte strings
   must either raise `LexError` or produce tokens — never crash and never silently drop a
   character (see the §9 MISMATCH lesson); (b) parse→`pretty()`→parse is a fixpoint on
   generated ASTs. Fixed seeds, runs in CI.

### 8.2 Layout

```
tests/
  conftest.py           # golden collector, VM factory (StringIO stdout), --update-goldens
  test_lexer.py  test_parser.py  test_checker.py  test_compiler.py
  test_vm.py  test_concurrency.py  test_pipeline.py  test_repl.py  test_fuzz.py
  golden/run/  golden/error/  golden/panic/
```

---

## 9. Salvage vs Discard (from the three old attempts)

**`Interpreter/` — discard nearly all.**
- Discard: `Tokenisation.py` wraps Python's stdlib `tokenize` (banned by the brief);
  `parse.py` expects `{`/`}` blocks while the token stream is Python-style — the exact
  stage-interface mismatch §6 is designed to kill; the classes are also dead on arrival
  from `_init_`/`_repr_` dunder typos (single underscores).
- Salvage (idea only): it was the sole attempt with a separate `SemanticAnalyser.py` —
  a distinct semantic pass before codegen is correct and survives as `checker.py`. Note it
  too leaned on Python's own `ast` (`AST.py` calls `ast.parse`; the analyser subclasses
  `ast.NodeVisitor`) — the same banned shortcut flagged below for `Final/`; only the
  *existence* of the pass is salvaged, not its substance.
- Also: it has a whole virtualenv (`Interpreter/Compiler/Lib/...`) committed inside the
  source tree — as does `Final/` (`Final/Final/`). New repo rule (two of three attempts
  needed it): venvs live outside the tree or in `.gitignore`d `.venv/`.

**`test compiler/` — the only real salvage.**
- Salvage: the master-regex table-driven lexer skeleton (`lexer.py`) is the right shape for
  our lexer core — keep the named-group `re.finditer` technique, add spans, keyword table,
  and the indentation layer on top. One required fix: the old token spec has no catch-all
  group, so `re.finditer` **silently skips illegal characters** (`@` just vanishes) — the
  new lexer ends the spec with `('MISMATCH', r'.')` and raises `LexError` on it. The
  `virtual_machine.py` dispatch shape (op → stack-effect handler) and operand order
  (`b, a = pop(), pop()`) carry into `vm.py`.
- Discard: no call frames (functions were Python callables injected from outside — not a
  language), variables in a single global dict, and the speculative
  `intermediate_code_generator.py`/`optimizer.py` stages. **v1 has no IR and no optimizer**;
  AST → bytecode directly. Those two files are exactly the scope creep that stalled it.

**`Final/` — discard entirely.**
- It parses with the `lark` library and evaluates by building Python's own `ast` — both
  explicitly banned. Nothing imports. Keep only two lessons: writing the grammar down first
  is worth doing (`Final/grammar.lark`, and `test compiler/grammer.txt` before it; note
  `Final/` also kept a stale near-duplicate `grammer.lark` plus a pasted copy of CPython's
  PEG grammar — keep exactly ONE grammar artifact; ours is §2.11, in the design doc itself),
  and fighting LALR conflicts in a grammar DSL consumed the attempt — our hand-written
  recursive descent gives error messages and control instead.

**Cross-cutting lesson all three teach**: each died between stages, not within one — token
shapes the parser didn't expect, ASTs the codegen didn't understand, and zero tests to
catch it. The literal exhibit is `Interpreter/main.py:21-23`: it feeds a *token list* into a
function whose contract is *source text*, and never invokes the analyser or compiler it
imports. Hence §6.2's frozen contracts and §8's pipeline smoke test from Milestone 1.

---

## Appendix A — Builtin registry (v1 complete list)

Builtins are callable names: referencing a *generic* builtin (or any opcode-backed
intrinsic) outside call position is a compile error in v1 (§2.2). A registry entry may
carry multiple signatures, tried in order (`len` is the only multi-entry builtin).

| Name | Signature | Notes |
|---|---|---|
| `print` | `(T) -> Unit` | any value; via `PRINT` op |
| `str` | `(T) -> Str` | top-level `render()`: `str(s)` on a Str is the identity — no quotes |
| `len` | `(List[T]) -> Int`, `(Str) -> Int` | two registered overloads |
| `push` | `(List[T], T) -> List[T]` | returns new list |
| `get` | `(List[T], Int) -> Option[T]` | safe index |
| `concat` | `(List[T], List[T]) -> List[T]` | |
| `range` | `(Int, Int) -> List[Int]` | half-open |
| `float` | `(Int) -> Float` | |
| `trunc` | `(Float) -> Int` | |
| `parse_int` | `(Str) -> Option[Int]` | |
| `split` | `(Str, Str) -> List[Str]` | |
| `substring` | `(Str, Int, Int) -> Str` | half-open, clamps |
| `chars` | `(Str) -> List[Str]` | |
| `chan[T](n?)` | `() -> Chan[T]` / `(Int) -> Chan[T]` | syntax form → `CHAN_NEW` |
| `send` | `(Chan[T], T) -> Unit` | opcode `CHAN_SEND` |
| `recv` | `(Chan[T]) -> Option[T]` | opcode `CHAN_RECV` |
| `close` | `(Chan[T]) -> Unit` | opcode `CHAN_CLOSE` |
| `sleep` | `(Int) -> Unit` | ms; opcode `SLEEP` |

## Appendix B — Rendering of values (`values.render`)

`render(v, nested=False)` has two modes: **top-level** (used by `print` and `str`) prints a
Str bare; **nested** (inside containers) prints it quoted using the §2.3 escapes. All other
values render identically in both modes: `42`, `3.14`, `true`, `()` ; `[1, 2]` ; `(1, "a")`
(List = Python `list` renders `[]`, Tuple = Python `tuple` renders `()` — distinct runtime
types make this decidable, §5.1) ; `Some(3)` / `None` / `Ok([1])` ; `Point(1.0, 2.0)` ;
`<fn add>` ; `<chan>`. Non-finite Floats have **no source literal** (they arise only from
IEEE-754 arithmetic, §2.3) but their rendered form is locked: `inf`, `-inf`, and `nan`.
Locked so golden tests are stable from day one.

## Appendix C — Error-code registry

Stable codes live as constants in `errors.py` (the code-constants block at the bottom of
that file is the machine-readable form of this table); ranges: `L0xx` lex, `P0xx` parse,
`E0xxx` check, `R0xx` runtime panic. Codes are **append-only and never reused**; every
rendered error and every golden `.err` file includes its code. Complete shipped registry
(v1.2 — matches `errors.py` one-for-one; grows via `DECISIONS.md`):

| Code | Name | Stage | Message / meaning |
|---|---|---|---|
| L001 | tabs-in-indent | lex | tabs are not allowed in indentation |
| L002 | bad-dedent | lex | unindent does not match any outer indentation level |
| L003 | illegal-char | lex | illegal character `{ch}` |
| L004 | invalid-escape | lex | invalid escape `\{ch}` |
| L005 | unterminated-string | lex | unterminated string literal |
| L006 | invalid-number | lex | invalid numeric literal (malformed, e.g. a misplaced underscore) |
| P001 | unexpected-token | parse | unexpected `{tok}`, expected {what} |
| P002 | unexpected-eof | parse | unexpected end of input, expected {what} |
| P003 | nesting-too-deep | parse | expression/type/pattern nesting too deep (recursion cap on every recursive entry point), or expression too long (flat operator chains are capped at 10_000 operands) |
| E0001 | unsupported | check | construct not available in this build/v1: bad `spawn` targets ("cannot spawn '{name}'; spawn a user-defined function or lambda"), generic `main`, unannotated block-bodied lambdas, `print` arity, match on a foreign (cross-module) enum |
| E0101 | duplicate-definition | check | duplicate definition of '{name}' / duplicate field '{name}' / '{name}' conflicts with an existing type or imported module (values, types, fields, variants-vs-values, type parameters) |
| E0102 | assign-immutable | check | cannot assign to immutable binding '{name}' |
| E0103 | var-capture | check | cannot capture/access mutable binding '{name}' from an enclosing scope; copy it into a 'let' first / cannot access mutable binding '{mod}.{name}' from another module |
| E0104 | non-bool-condition | check | condition must be Bool |
| E0105 | non-exhaustive-match | check | match on '{type}' is not exhaustive; missing: {witness} |
| E0106 | question-mark-mismatch | check | '?' misuse: Option/Result vs the function's return type, '?' on a non-Result/Option, '?' outside a function / in an unannotated lambda |
| E0107 | cannot-infer-empty-list | check | cannot infer element type of []; add an annotation |
| E0108 | missing-return-path | check | missing return path — function must return {R} on all paths |
| E0109 | intrinsic-not-value | check | intrinsic '{name}' is not a first-class value / cannot rebind prelude name '{name}' |
| E0111 | unknown-name | check | unknown name '{name}' |
| E0112 | type-mismatch | check | expected {T}, found {U} (the general operator/argument/branch/arm mismatch, incl. select head "expects a channel") |
| E0113 | not-comparable | check | '{op}' is not defined for {T} (ordering beyond Int/Float/Str; `==` beyond primitives) |
| E0114 | loop-control | check | 'break'/'continue' outside a loop |
| E0115 | unknown-type | check | unknown type '{name}' (also wrong/missing type arguments on a type) |
| E0116 | return-outside | check | 'return' outside a function |
| E0117 | not-callable | check | {T} is not callable |
| E0118 | arity | check | wrong number of arguments (call/spawn arity mismatch) |
| E0119 | cannot-infer | check | cannot infer type argument '{T}' …; add an annotation (underdetermined generic/builtin instantiation) |
| E0120 | unreachable-arm | check | unreachable match arm |
| E0121 | not-a-variant | check | '{name}' is not a variant of {enum} |
| E0122 | variant-arity | check | variant pattern/constructor has the wrong number of fields |
| E0123 | constructor-not-value | check | variant/struct constructor is not a first-class value (call-position only) |
| E0124 | dup-variant | check | duplicate variant name '{name}' within an enum |
| E0130 | import-cycle | check | import cycle: a -> b -> a |
| E0131 | module-not-found | check | cannot find module '{name}' ({name}.tn) |
| E0132 | not-a-module-member | check | module '{m}' has no member '{name}' |
| E0133 | module-not-value | check | module '{name}' is not a value |
| E0134 | import-unsupported | check | import is only available when running a source file (not stdin/REPL) |
| E0135 | select-else | check | select: 'else' must be the single, last arm |
| E0136 | generic-not-value | check | generic function '{name}' is not a first-class value (call it — type arguments are always inferred — or wrap it in a lambda) |
| R001 | div-by-zero | run | division or modulo by zero |
| R002 | index-out-of-range | run | list index out of range |
| R003 | send-on-closed | run | send on closed channel |
| R004 | close-closed | run | close of closed channel |
| R005 | deadlock | run | deadlock — every fiber is blocked |
| R006 | uninitialized | run | value used before initialization (module init order / checker-bug backstop) |
| R007 | match-fail | run | non-exhaustive match (the `MATCH_FAIL` safety net — should be unreachable) |
| R008 | step-limit | run | execution step limit reached ({n} instructions) — only when `VM(step_limit=…)` is set |
| R009 | builtin-error | run | {builtin}: {detail} — a builtin's domain error (wrapped host exception) panics at the call site |
| R010 | stack-overflow | run | call stack overflow ({cap} frames) |
| R011 | bad-capacity | run | channel capacity cannot be negative ({cap}) |

Retired numbers: **E0110** (the v1.1 seed table's `spawn-builtin`) was never minted in the
implementation — spawn-of-builtin ships under E0001 — and, per the never-reuse rule, the
number stays retired. E0125–E0129 are unassigned gaps left for future check-stage codes.
