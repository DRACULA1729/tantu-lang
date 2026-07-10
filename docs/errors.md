# Error codes

Every error Weft reports carries a stable code. This page lists them by family: `L` lex, `P`
parse, `E` check, `R` runtime panic. Codes are append-only and never reused. The list in
`weft/errors.py` is the machine-readable form; `DESIGN.md` Appendix C is authoritative if the
two ever disagree.

## L — lexer

| Code | Message | What it means / typical fix |
|---|---|---|
| L001 | tabs are not allowed in indentation | Indent with spaces. |
| L002 | unindent does not match any outer indentation level | A dedent landed between two indentation levels; line up with an enclosing block. |
| L003 | illegal character | A character that isn't part of any token. Remove it. |
| L004 | invalid escape | Only `\n`, `\t`, `\r`, `\\`, and `\"` are valid string escapes. |
| L005 | unterminated string literal | A `"` with no closing quote on the same line. Strings are single-line. |
| L006 | invalid numeric literal | A misplaced underscore (`1_`, `1__0`). An underscore must sit between two digits. |

## P — parser

| Code | Message | What it means / typical fix |
|---|---|---|
| P001 | unexpected token, expected ... | The parser found a token it can't use here. The message says what it wanted. |
| P002 | unexpected end of input, expected ... | The program ended mid-construct, e.g. an unclosed bracket or a missing body. |
| P003 | expression/type/pattern nesting too deep | Nesting exceeded the recursion cap. Almost always malformed or generated input. |

## E — checker

| Code | Message | What it means / typical fix |
|---|---|---|
| E0001 | construct not available in this build / v1 | Something the language doesn't support: a bad `spawn` target, a generic `main`, an unannotated block-bodied lambda, wrong `print` arity, or `match` on a foreign (cross-module) enum. |
| E0101 | duplicate definition | A name (value, type, variant, or type parameter) is declared twice in the same scope. |
| E0102 | cannot assign to immutable binding | The target is a `let`. Declare it `var` if it needs to change. |
| E0103 | cannot capture/access mutable binding from an enclosing scope | A function or another module read a `var`. Copy it into a `let` first. Keeps fibers from sharing mutable state. |
| E0104 | condition must be Bool | The test in an `if`/`while`/etc. isn't `Bool`. |
| E0105 | match is not exhaustive | Add the missing arm shown in the message, or a wildcard `_`. |
| E0106 | `?` misuse | `?` used on a non-`Option`/`Result`, against the wrong return type, outside a function, or inside an unannotated lambda. |
| E0107 | cannot infer element type of `[]` | Annotate the empty list, e.g. `let xs: List[Int] = []`. |
| E0108 | missing return path | A function with a declared return type can fall off the end without returning. |
| E0109 | intrinsic is not a first-class value | A builtin/intrinsic used as a value, or an attempt to rebind one. Call it, or wrap it in a lambda. |
| E0111 | unknown name | The name isn't in scope. Check spelling and imports. |
| E0112 | type mismatch (expected ..., found ...) | The general operator/argument/branch/arm mismatch, including a `select` head that isn't a channel. |
| E0113 | operator not defined for this type | Ordering beyond `Int`/`Float`/`Str`, or `==`/`!=` beyond primitives. |
| E0114 | `break`/`continue` outside a loop | Move it into a loop. |
| E0115 | unknown type | The type name doesn't exist, or a type has the wrong or missing type arguments. |
| E0116 | `return` outside a function | Move it into a function body. |
| E0117 | not callable | You called something that isn't a function. |
| E0118 | wrong number of arguments | Call or `spawn` arity doesn't match the signature. |
| E0119 | cannot infer type argument | A generic or builtin instantiation is underdetermined. Add an annotation. |
| E0120 | unreachable match arm | An earlier arm already covers this one. Remove or reorder it. |
| E0121 | not a variant | The name isn't a variant of that enum. |
| E0122 | variant pattern/constructor has the wrong number of fields | Match the variant's declared arity. |
| E0123 | variant/struct constructor is not a first-class value | Call-position only. Wrap it in a lambda to pass it around. |
| E0124 | duplicate variant name within an enum | Rename one variant. |
| E0130 | import cycle | Modules import each other in a loop. Break the cycle. |
| E0131 | cannot find module | No `name.weft` sibling for the `import`. |
| E0132 | module has no member | The qualified name isn't exported by that module. |
| E0133 | module is not a value | A module name used as a value. Qualify it with a member. |
| E0134 | import is only available when running a source file | Imports don't work from stdin or the REPL. |
| E0135 | `select` `else` must be the single, last arm | Move the `else` arm to the end; there can be only one. |
| E0136 | generic function is not a first-class value | Call it (type arguments are always inferred) or wrap it in a lambda. |

## R — runtime panic

| Code | Message | What it means / typical fix |
|---|---|---|
| R001 | division or modulo by zero | The right operand of `/` or `%` was zero. |
| R002 | list index out of range | An index outside `0 .. len - 1`. |
| R003 | send on closed channel | A `send` (or a first-ready select send arm) on a closed channel. |
| R004 | close of closed channel | A channel was closed twice. |
| R005 | deadlock — all fibers are blocked | Every fiber is waiting and none can proceed. |
| R006 | value used before initialization | A module-level value read before its initializer ran, or a checker-bug backstop. |
| R007 | non-exhaustive match | The `MATCH_FAIL` safety net; should be unreachable given the E0105 check. |
| R008 | execution step limit reached | Only when the VM is run with a `step_limit` (e.g. an untrusted playground). |
| R009 | builtin domain error | A builtin's host exception, caught at the call site (e.g. `split(s, "")`, `trunc(NaN)`). |
| R010 | call-stack depth cap exceeded | Recursion ran past the VM's frame cap — usually unbounded recursion. |
| R011 | negative channel capacity | `chan[T](n)` with `n < 0`. |

Retired: **E0110** was listed in the v1.1 seed table but never minted (spawn-of-builtin ships
as E0001); the number stays retired. E0125–E0129 are unassigned gaps for future check codes.
