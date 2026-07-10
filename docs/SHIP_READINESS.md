# Weft — Ship-Readiness Handoff

**Status when this was written:** M1–M9 complete, 281/281 tests green, 91% coverage, all 12 examples run, `pip install .` works. No code has been changed — this is a plan.

**Contract:** `DESIGN.md` v1.1 (single source of truth) + `DECISIONS.md`.

**How this doc was produced:** four independent audits (front-end, type system, back-end/VM, release engineering) + one planning pass, each finding reproduced against the real binary. Every `file:line` below was re-verified against the current tree.

**Breaking-risk legend (read before starting an item):**
- 🟢 **additive** — new guards / catches / features; cannot reject a program that compiles today.
- 🟡 **behavior-change** — intentionally *rejects* code that currently compiles (a spec violation being closed). Audit your own examples first.
- 🟠 **output-sensitive** — must keep golden output byte-identical / touches a hot path; full suite is the guardrail.

---

## Release gate — the minimum to ship

**Correctness blockers**
- [ ] No input (file / stdin / REPL line) can produce a raw Python traceback.
- [ ] The `?`-in-unannotated-lambda soundness hole is closed.
- [ ] `match` on an imported enum gives a clean diagnostic, not `KeyError`.
- [ ] REPL accepts a multi-statement block and survives Ctrl-C.

**Legal / release blockers**
- [ ] `LICENSE` present; pyproject has license/authors/classifiers.
- [ ] CI green on `main` running the full suite + all 12 examples (M8 acceptance literally requires "runs in CI").
- [ ] `pip install .` provides a `weft` command.
- [ ] `DESIGN.md` v1.2 matches the shipped language (select + generics grammar).
- [ ] The 53 MB of untracked prototypes can't be accidentally committed.

Everything else (TS-MAJOR-3/4/5, BE-MAJOR-3/4/5, minors) is strongly recommended for the same release but is not gate-blocking if you choose to document instead of fix.

---

## Three fix-shape corrections (the naive fix leaves the bug open)

1. **Parser depth guard (W4):** a counter in `expression()` alone misses 3 of 4 repros — `not `×400 recurses `_not→_not`, unary `-` recurses `_unary→_unary`, pattern/type nesting never re-enters `expression()`. Guard **all five** entry points.
2. **Lambda soundness (W2):** guarding `_try` alone leaves the hole open via `return` inside a `match` in an inline lambda body — `_return` reads the same `ret_stack[-1]`. Use an `_INFER` sentinel guarding **both** `_try` and `_return`.
3. **Cross-module `var` (W13):** checking mutability in `_field` is impossible because `check()` drops mutability at export (checker.py:1294). Carry `mutable` through `module.symbols` first.

Also: FE unit-literal `()` and block-if are **implementation gaps, not spec gaps** — §2.11 already contains both productions. So DESIGN v1.2 is mostly transcription (select + `[T,...]`), not new design.

---

## Phase 0 — Harness first (land at the known-green baseline)

### W0 — CI + branch + license + packaging — 🟢 S
Resolves **CC-BLOCKER-1, CC-BLOCKER-3, CC-MAJOR-2, CC-MAJOR-4**.
- `LICENSE` — MIT, copyright the **DRACULA1729** OSS identity. ⚠️ This is the OSS repo: **no `Co-Authored-By: Claude` line** in commits; spot-check `git config user.email` (the identity guard hook will block a wrong-identity write).
- `pyproject.toml`:
  - `[project.scripts]` → `weft = "weft.cli:main"` (console-scripts pass `main()`'s int return to `SystemExit`; no shim needed).
  - add `license = {file = "LICENSE"}`, `authors`, `classifiers`, `readme = "README.md"`, `[project.urls]`.
  - single-source the version: `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {attr = "weft.__version__"}` — kills the pyproject/`weft/__init__.py:9` duplication.
- `.github/workflows/ci.yml`: Python 3.12 (+3.13), `pip install -e .[dev]`, `pytest`, then a loop running `weft check` + `weft run` over `examples/*.weft` asserting exit 0 (the M8 criterion, currently only tested in-process).
- Create `main` from `source`, push, set default.
- `.gitignore`: add `/Interpreter/`, `/Final/`, `/test compiler/`, `gpt_chat_links.txt`, `build/`, `weft.egg-info/`, `.coverage`.
- **Test:** `tests/test_packaging.py` smoke — `python -m weft --version` via subprocess.

---

## Phase 1 — The exception boundary (one fix, many findings)

### W1 — CLI + REPL catch-all boundary — 🟢 S/M
Resolves **FE-BLOCKER-2** fully; the traceback half of **FE-BLOCKER-1**; **FE-7**; backstops **BE-BLOCKER-1/2**. Highest-leverage change: after it, every remaining bug degrades to a clean one-line diagnostic and the REPL session survives.
- `cli.py` — module-level helper:
  ```python
  def _internal_error(err, exc) -> int:
      err.write(f"weft: internal error: {type(exc).__name__}: {exc}\n"
                "  this is a bug in weft — please report it\n")
      return 2
  ```
  Wrap the command dispatch tail of `main()` (cli.py:197–209) in `try / except (KeyboardInterrupt, SystemExit): raise / except Exception: return _internal_error(...)`.
- UTF-8 (clean message, not internal-error path):
  - `_read_target` (cli.py:162–170): catch `(OSError, UnicodeDecodeError)`; decode case → `weft: {target}: source is not valid UTF-8 ({e})`, return `None` → exit 2. Move the `target == "-"` stdin read **inside** the try (covers cli.py:164).
  - `build_program` (cli.py:136): add `except UnicodeDecodeError` sibling to the existing `except OSError` (covers the open at cli.py:99).
  - cli.py:204 (`weft run -` stdin): route through `_read_target("-")` instead of the bare `sys.stdin.read()`.
- `repl.py:85–90`: add `except KeyboardInterrupt: stdout.write("\nKeyboardInterrupt\n"); continue` (around both `readline` and `run_input`) and a final `except Exception as e:` writing the internal-error line and continuing. The session must never die.
- **Tests:** new `tests/test_cli.py` — subprocess: Latin-1 file + binary stdin → exit 2, assert `"Traceback" not in output`. `tests/test_repl.py` — feed raising input, assert session continues. Add the invariant helper: for every crash repro in this plan, assert no `"Traceback"`.

---

## Phase 2 — Soundness + specific crash fixes (before any behavior-locking test)

### W2 — Lambda return-frame sentinel — 🟢 S
Resolves **TS-BLOCKER-1** (+ the bogus module-level E0106). *Additive: it only rejects the unsound program, which crashes at runtime today.*
- `checker.py`: module-level `_INFER = object()`.
- `_lambda` inline branch (checker.py:628–629):
  ```python
  self.ret_stack.append(_INFER)
  ret = self.synth(e.body[0].expr, fn_scope)
  self.ret_stack.pop()
  ```
- `_try` (checker.py:1153–1156): after the empty-stack check, `if self.ret_stack[-1] is _INFER:` → E0106 `"'?' inside a lambda without a return-type annotation; add '-> T'"`, return ERROR. (Also fixes the wrong module-level message.)
- `_return` (checker.py:383): `if R is _INFER:` → error `"'return' inside a lambda without a return-type annotation; add '-> T'"`, synth the value, return.
- **Tests:** `tests/test_checker.py` — (a) the audit repro (Err escaping as payload) now checks *dirty*; (b) `?` in an annotated lambda still works; (c) `return` inside match inside inline lambda errors; (d) module-level inline-lambda `?` gets the new message. Golden `tests/golden/error/lambda_question.{weft,err}`.

### W3 — Foreign-enum match guard — 🟢 S
Resolves **TS-BLOCKER-2**. Per DECISIONS M8 (match on foreign types deferred post-v1), reject cleanly — do not attempt cross-module exhaustiveness (decision **D2**).
- `_check_match` (checker.py:851, right after `S = self.synth(...)`):
  ```python
  if isinstance(S, EnumT) and S.name not in self.enums:
      self.error(E_UNSUPPORTED, f"cannot match on foreign enum type '{S.name}' "
                 f"(cross-module enums are not supported in v1)", m.scrutinee.span)
      S = ERROR
  ```
  `S = ERROR` makes the existing arm/exhaustiveness paths (checker.py:858/864) degrade safely. Belt-and-braces: in `exhaustive.py`, use `registry.get(...)` and treat a miss as incomplete rather than crashing (kills the `exhaustive.py:54` KeyError permanently).
- **Tests:** `tests/test_modules.py` — lib exports enum-typed value, main matches it (with and without `_`) → clean E-code, exit 2. Golden under `tests/golden/modules/`.

### W4 — Parser nesting-depth guard — 🟢 S
Resolves the guard half of **FE-BLOCKER-1**. *See correction #1 — guard all five entry points.*
- `parser.py`: `self.depth = 0` in `__init__`; `_MAX_DEPTH = 100`; helper:
  ```python
  def _deeper(self):
      self.depth += 1
      if self.depth > _MAX_DEPTH:
          raise ParseError(P_TOO_DEEP, "expression nesting too deep", self.peek().span)
  ```
  Increment/decrement (try/finally) in `expression()`, `_not()`, `_unary()`, `_pattern()`, `_type()`.
- New code `P003` in `errors.py` (append-only registry; goes into Appendix C in W8). Cap 100 keeps checker/compiler (~3–5 Python frames per AST level) under CPython's limit; W1 backstops anything that slips.
- **Tests:** `tests/test_parser.py` — all four repros (`(`×100, `not `×400, `[`×100, `-`×400) → `ParseError P003`; subprocess variant in `test_cli.py` (exit 2, no traceback); REPL variant in `test_repl.py` (session survives).

### W5 — Builtin panic boundary + total builtins — 🟢 S/M
Resolves **BE-BLOCKER-1**.
- Backstop at dispatch — `vm.py:329`:
  ```python
  try:
      result = self.builtins[callee.name](*args)
  except WeftPanic: raise
  except Exception as ex:
      raise WeftPanic(R_BUILTIN, f"{callee.name}: {ex}", frame.code.spans[frame.ip - 1])
  stack.append(result)
  ```
  New `R_BUILTIN = "R009"` ("builtin domain error"). This alone kills every raw traceback from prelude code.
- Make the four known cases *defined* (`prelude.py:79–95`):
  - `float(n)`: `try: float(n) except OverflowError: math.inf` — §2.3 says Float overflow yields `inf`, a legal value.
  - `trunc(x)`: on `isnan`/`isinf`, raise a `ValueError` with a good message; the boundary wraps it into R009 with the span.
  - `split(s, "")`: define as `chars(s)` (decision **D7**; panic is the fallback).
- **Tests:** `tests/test_vm.py` + `tests/golden/panic/` — `split("abc","")`, `float(10**500)` → `inf`, `trunc(1e300*1e300)` → R009 with trace, `trunc(NaN)` → R009. Assert panic render (code + caret), exit 1.

### W6 — Iterative `render()` — 🟠 M
Resolves **BE-BLOCKER-2**. *Output must stay byte-identical — the Appendix B goldens are the guardrail.*
- `values.py:65–92`: rewrite as an explicit work-stack / token-emitter. Each compound value (list/tuple/Variant-with-fields/Record) pushes a frame `(prefix, children_iter, sep, suffix)`; primitives append their string. Preserve exactly: top-level-vs-nested Str quoting, the `bool`-before-`int` check, `Variant` without fields → bare name. ~40 lines.
- **Tests:** all existing Appendix B goldens must pass unchanged (that *is* the regression test). Add `tests/test_vm.py::test_render_deep_recursive_value` — build a 5000-deep cons list, `print` it, assert exit 0 (RecursionError at ~400 today).

---

## Phase 3 — REPL (M1 headline deliverable)

### W7 — REPL continuation rewrite — 🟢 M
Resolves **FE-MAJOR-3, FE-6, FE-7, CC-BLOCKER-2**.
- `repl.py:41–62`: replace "re-test the whole buffer" with the §6.4 rule — the decision to *enter* continuation is made once, on the **first line**; once entered, read until a **blank line** (or EOF):
  ```python
  buf = line
  if _opens_block(_strip_comment(line)) or _open_brackets(buf):
      while True:
          # prompt "... "; cont = readline()
          if cont == "" or cont.strip() == "": break
          buf += "\n" + cont
  ```
  Fixes both directions: (a) multi-statement fn / multi-arm match / if-else become enterable (blank line submits, not the first body line); (b) a buffer whose last line ends in `:` no longer loops to EOF. `_strip_comment` (string-aware, reuse the existing in-string scanner) fixes FE-6 (a `# note (` line no longer wedges).
- Ctrl-C handled in W1.
- **Tests:** `tests/test_repl.py` (this is CC-BLOCKER-2's "structurally hidden" complaint — the suite currently has **zero** multi-line REPL tests): multi-statement `fn` defined then called; multi-arm `match`; `if/else` block; a comment containing `(` and one ending in `:`; a buffer ending in `:` then blank line terminates; bracket continuation. ~6 tests via the existing StringIO driver.

---

## Phase 4 — DESIGN v1.2 + the two missing productions (spec and code land together)

### W8 — DESIGN.md v1.2 — 🟢 M (editing)
Resolves **CC-MAJOR-1** + carries the new codes minted in W4/W5/W17/W18/W19 + BE-MINOR-7 doc.
- §4/§501: delete "select is post-v1"; describe shipped M9a select (source-order poll, else-arm, closed-channel semantics per DECISIONS).
- §2.11 grammar: add `select_expr` / `select_arm` productions; add optional `[ "[" IDENT { "," IDENT } "]" ]` to `fn_decl`/`enum_decl`/`struct_decl` (the edit DECISIONS M8:67 claims was made but wasn't).
- Appendix C: refresh to match `errors.py` in full — E0106/E0108 messages, E0111–E0136, R006–R008, and new P003/R009/R010/R011/L006.
- Appendix B: add `inf`/`nan` as the locked rendering of non-finite Floats (decision **D9**).
- §6.4: note blank-line termination applies to bracket continuations too (W7).
- Bump header to v1.2; update `weft/__init__.py` docstring "(v1.1)"; add a DECISIONS.md row.
- Optional CI check: grep that every code in `errors.py` appears in Appendix C.

### W9 — Unit literal `()` — 🟢 S
Resolves **FE-MAJOR-4 / TS-MINOR-7**.
- `ast_nodes.py`: add `UnitLit(span)`.
- `parser.py:630–632` (`_primary` LPAREN): after `lp = self.advance()`, `if self.check(RPAREN): rp = self.advance(); return A.UnitLit(Span.merge(lp.span, rp.span))`.
- `checker.py` `_synth`: `UnitLit` → `UNIT`.
- `compiler.py`: emit `LOAD_CONST None`.
- **Tests:** `tests/test_parser.py`, `tests/test_pipeline.py` — `let u = ()`, `print(())` → `()`.

### W10 — If-expr in statement/tail position + block-form if-expr — 🟠 M/L
Resolves **TS-MAJOR-6 / FE-MAJOR-5** (§2.11 already promises both). *Parser-ambiguity area — medium risk.*
- **(a) inline `if c: expr else: expr` in statement position** (§2.8's own example): in `if_statement()` (parser.py:208–224), after `expect(COLON)`, for an inline branch parse `self.expression()` when lookahead isn't a statement keyword; if the following token is `ELIF`/`ELSE` (no NEWLINE consumed), commit to the if-**expression** path and return `A.ExprStmt(IfExpr)`; if NEWLINE, wrap as the single-statement block of a normal `A.If`. LL with one committed expression — no backtracking.
- **(b) block-form if-expr** (`let y = if x > 0:` NEWLINE INDENT …): extend `A.IfExpr` so each branch body is `list[A.Stmt]` (inline → 1-element `[ExprStmt]`). `_if_expr` (parser.py:485–499): after each `expect(COLON)`, `if self.check(NEWLINE): body = self._block() else: body = [ExprStmt(expression())]`. Checker: evaluate each body with the existing block-as-value machinery (`_arm_value` already does "stmts then tail expression + expected-type flow + divergent-arm handling"). Compiler: `_if_expr` (compiler.py:530) compiles each body via the existing `_arm_body_value` (compiler.py:464). `simple_statement` (parser.py:142) already handles the trailing-block-form-RHS DEDENT.
- **Tests:** `tests/test_parser.py` (both forms, `elif` chains, tail position), `tests/test_checker.py` (branch type agreement, expected-type flow), goldens for DESIGN §2.6 and §2.8 **verbatim** examples — those compiling is the acceptance test.

---

## Phase 5 — Type-system majors

### W11 — Expected-type flow into call args / list / tuple elements — 🟠 M
Resolves **TS-MAJOR-3**. *Only accepts-more (turns E0119 noise into successes); rejections can't newly appear except where an arg previously synthesized to a wrong-but-coincidentally-equal type — a latent bug you want surfaced. Lands early so CI soaks it before the tag.*
- `_call` (checker.py:662–676): synth the callee first; if `fty` is `FnT` with matching arity, `check_expr(a, pty, scope)` per arg instead of synth-then-`require`; on mismatch/non-FnT, fall back to synthesizing args (preserves errors).
- `_spawn` (checker.py:1241–1246): same for the arg loop.
- `_list_lit` (checker.py:1028–1035): keep synth-first-element for the synth path, but `check_expr` the rest against `elem`; **add** a `check_expr` case for non-empty `ListLit` with `ListT` expected (~checker.py:421) checking every element against `expected.elem` (makes `let xs: List[Option[Int]] = [None]` work).
- `TupleLit`: add a `check_expr` case — `TupleT` expected with matching arity → `check_expr` pairwise; else fall through to synth+require.
- **Tests:** `tests/test_checker.py` — the two repros pass; nested (`f(Some(None))`); a genuinely ambiguous `None` still E0119s. Update `examples/options.weft` if it carries noise annotations.

### W12 — Name-hygiene sweep — 🟡 M
Resolves **TS-MAJOR-4** (all 7 bypasses, one helper). ⚠️ **Behavior-change: rejects programs that compile today** (duplicate params/fields, shadowed prelude/reserved names, name collisions). Audit your examples/tests first — this is spec compliance (§2.2/§2.7), but anything relying on the bypasses breaks.
- `checker.py` — one helper:
  ```python
  def _declare_checked(self, scope, name, ty, mutable, span, *, generic=False):
      if name in _RESERVED or name in _PRELUDE_NAMES:
          self.error(E_INTRINSIC_NOT_VALUE, f"cannot rebind prelude name '{name}'", span)
      if name in self.module_names:
          self.error(E_DUPLICATE, f"'{name}' conflicts with imported module '{name}'", span)
      prior = scope.local(name)
      if prior is not None and not scope.repl:
          self.error(E_DUPLICATE, ...)
      return scope.declare(name, ty, mutable, span, generic=generic)
  ```
  `_PRELUDE_NAMES = {"Some","None","Ok","Err","Option","Result"}` (decision **D6**).
  Route through it: `_let` (335–341), `hoist_fn` (213–219), fn params (245–246), lambda params (619–620), `_for` binder (1094), `_check_let_tuple` (1115) + an intra-tuple dup check, match `PIdent` binders (checker.py:987 — add the reserved/prelude check but **skip** the `scope.local` dup check there, since sibling arms legitimately reuse names — only same-pattern dups matter), select binders (927–932, converge on the helper).
- `register_struct` (224–231): per-field dup check → E0101 "duplicate field".
- Cross-namespace collisions (§2.7): extend the pass-0 census (checker.py:1269–1276) with fn names, then each registrar checks the census (variant vs fn/struct, fn vs variant/struct).
- Import-name shadowing: covered by the `module_names` clause.
- **Tests:** `tests/test_checker.py` — one test per bypass (7+) asserting the E-code; positive tests that legit inner-scope shadowing and same-name binders across different match arms still work.

### W13 — Cross-module `var` read ban — 🟡 S
Resolves **TS-MAJOR-5**. *See correction #3.* ⚠️ Rejects `foreign.var` reads that compile today (restores §4.1's "no shared mutable state by construction").
- checker.py:1294: `module.symbols = {name: (info.slot, info.ty, info.mutable) for ...}`.
- Update the two unpack sites — `_generic_callee` (149) and `_field` (1055) — to 3-tuples; in `_field`, before resolving: `if mutable: self.error(E_VAR_CAPTURE, f"cannot access mutable binding '{obj}.{name}' from another module", span); return ERROR` (E0103, unconditionally).
- **Tests:** `tests/test_modules.py` — `lib` exports `var v`; reading `lib.v` from a fn and from module level → E0103; `let` member still works.

### W14 — Match error-cascade suppression — 🟢 S
Resolves **TS-MINOR-8**.
- `_check_match` (855–862): snapshot `n = len(self.errors)` before `_check_pattern`; if it grew, don't append the pattern's row to `rows`, skip its `is_useful` check, and set `had_pattern_error` to also suppress the exhaustiveness check at 864–869.
- **Tests:** `tests/test_checker.py` — the repro reports exactly the root error, zero E0120s.

---

## Phase 6 — VM resource correctness

### W15 — Select waiter dequeue-on-wake — 🟠 M
Resolves **BE-MAJOR-3**. *Touches the M9a hot paths; the extensive "clean" select/close suite is the safety net.*
- `vm.py`: `_SelectGroup` gains `entries: list[tuple[deque, _SelectWaiter]]`; append `(deque, waiter)` per arm at registration (527–531). Helper:
  ```python
  def _cancel_select(self, group):
      group.won = True
      for dq, w in group.entries:
          try: dq.remove(w)
          except ValueError: pass
      group.entries.clear()
  ```
  Call it everywhere `group.won = True` is set today: `_resolve_recv` (213), `_resolve_send` (222), and the CHAN_CLOSE skims (458–477). Keep the tombstone-skim in `_pop_live_*` as a second line of defense.
- **Tests:** `tests/test_select.py` — the server-loop repro (N selects over `{data, shutdown}`, shutdown never fires); after N messages assert `len(shutdown.recv_wait) <= 1` (white-box VM test — the suite already does these). Re-run all select/close goldens unchanged.

### W16 — Fiber registry pruning — 🟢 S
Resolves **BE-MAJOR-4**.
- `vm.py:139`: make `self.registry` a `dict[int, Fiber]`; SPAWN (416) and main (143) set `registry[f.fid] = f`; the DONE branch (172–175) does `registry.pop(fib.fid, None)` before the `fid == 0` check; deadlock scan (156) iterates `registry.values()`. O(1) removal (no list `remove` — that'd be O(n²) on the 50k repro).
- **Tests:** `tests/test_concurrency.py` — spawn 10k sequential short fibers, assert `len(vm.registry)` small at exit; R005 deadlock goldens unchanged.

### W17 — Weft call-depth cap — 🟢 S
Resolves **BE-MAJOR-5**.
- `vm.py` CALL closure branch (319–325), before `fiber.frames.append(nf)`:
  ```python
  if len(fiber.frames) >= self.max_frames:
      raise WeftPanic(R_STACK_OVERFLOW, f"call stack overflow ({self.max_frames} frames)",
                      frame.code.spans[frame.ip - 1])
  ```
  `self.max_frames` a VM ctor param, default per decision **D3** (recommend 100 000). New `R_STACK_OVERFLOW = "R010"`. Cap the rendered trace (innermost ~20 + "… N more frames") in `_trace`/`render_panic`.
- **Tests:** golden `tests/golden/panic/stack_overflow.weft` (`fn f(n: Int) -> Int: return f(n+1)`) → R010, exit 1, truncated trace; a deep-but-legal recursion (e.g. 50k) still succeeds. ⚠️ Check no existing test/example recurses past the cap before finalizing the default.

### W18 — Negative channel capacity — 🟢 S
Resolves **BE-MINOR-6**.
- `vm.py` CHAN_NEW (418–419): `cap = stack.pop(); if cap < 0: raise WeftPanic(R_BAD_CAP, ...)`. New `R011`. DECISIONS row (Go panics; DESIGN silent).
- **Test:** golden panic case `chan[Int](-5)`.

---

## Phase 7 — Front-end minors + coverage + docs (batchable)

### W19 — Lexer minors — 🟢/🟡 S
Resolves **FE-MINOR-8, FE-MINOR-10**.
- BOM: strip at the **read boundary**, not in `tokenize` (stripping in the lexer desyncs spans from the `src` the CLI hands to `render_error`): open with `encoding="utf-8-sig"` in `_read_target`/`build_program`; for stdin/REPL `if src.startswith("﻿"): src = src[1:]`. 🟢
- Underscores: after `_number` builds `text` (lexer.py:170), linear scan (`_` must sit between digits) → new `L006 "invalid numeric literal"`. 🟡 (`1_`, `1__0` currently lex as 1/10 — decision **D8**).
- **Tests:** `tests/test_lexer.py` — BOM'd hello runs; `1_`, `1__0` → L006.

### W20 — Friendly layout-token names — 🟢 S
Resolves **FE-MINOR-9**.
- `parser.py:69` `_unexpected`: map `{NEWLINE: "end of line", INDENT: "an indented block", DEDENT: "end of block"}` before falling back to `tok.type.name`.
- **Test:** one golden error message updated/added.

### W21 — CLI test coverage — 🟢 M
Resolves **CC-MAJOR-3** (cli.py 47% → >90%).
- Extend `tests/test_cli.py`: drive `main(argv)` in-process with monkeypatched stdin/stdout/stderr for arg parsing, `--version`, unknown command → 2 + usage, missing file arg, `run/check/dis` × file/stdin, exit codes 0/1/2 and stdout-vs-stderr routing (frozen §6.2 contract); plus 2–3 true subprocess tests so the user-facing surface is exercised out-of-process.

### W22 — README / CHANGELOG / hygiene finish — 🟢 S
Resolves **CC-MINOR-1/2/3** + remainder of **CC-MAJOR-4**.
- README: replace literal "277 tests" with a non-rotting phrasing + CI badge; add the `pip install`/`pipx` non-dev path and the `weft` command.
- `CHANGELOG.md`: a `0.1.0` entry summarizing M1–M9 + this hardening pass. Tag `v0.1.0` after the gate is green.
- Prototypes: execute decision **D5** (recommend moving `Interpreter/`, `Final/`, `test compiler/`, `gpt_chat_links.txt` out of the working tree — a sibling attic dir — since gitignored-but-present 53 MB still bloats every grep/clone).
- Optional (non-gating): `docs/tour.md` bridging the README → DESIGN gap.

---

## Sequencing spine

```
W0 (harness)              ─► everything runs under CI; license unblocks "public" at all
W1 (exception boundary)   ─► W4/W5/W6 use it as backstop; prerequisite for the "no traceback" tests
W2,W3 (soundness/crash)   ─► before behavior-locking tests (else tests enshrine unsound acceptance)
W4 (depth guard)          ─► completes FE-BLOCKER-1 with W1
W5,W6 (VM tracebacks)     ─► goldens here feed the CI examples gate
W7 (REPL)                 ─► independent after W1; early because it's the M1 headline UX
W8 (DESIGN v1.2)          ─► settle spec text before W9/W10; stays open until Phase 6 codes are final
W10 (if-expr)             ─► before W11 (§2.8's example is both a parser and an inference fix — test once)
W11 (arg flow)            ─► early after prereqs so CI soaks the inference change before tag
W12–W14                   ─► independent; after W11 to avoid double-churn in _call/_let
W15–W18 (VM resources)    ─► independent of checker work; W15 before W16 (both touch scheduler; W15 riskier)
W19–W22                   ─► anytime after W1; batched last as polish
```

Guiding order: (1) the boundary protects users, so it's first; (2) soundness before tests-that-lock; (3) spec before syntax; (4) behavior-changing inference early for soak; (5) leaks/caps follow the crash class (they only bite long-running programs).

---

## Decisions to make (recommendation in bold)

| # | Decision | Recommendation | Tradeoff |
|---|---|---|---|
| D1 | R-codes R009/R010/R011 | **Mint all three, append-only** | one shared code is smaller but kills per-class golden testability |
| D2 | foreign-enum match | **Reject (E-code)** per DECISIONS M8 | cross-module exhaustiveness = real post-v1 feature work |
| D3 | frame-depth cap default | **100 000** | lower catches runaways sooner but breaks legit deep recursion (200k works today) — verify no test exceeds it |
| D4 | DESIGN v1.2 vs demote authority | **v1.2** | the doc is the project's best asset; v1.2 costs a careful editing day |
| D5 | prototypes | **Move out of the working tree** (+ keep W0 gitignore) | an attic branch preserves them in-repo but ships 53 MB to every clone |
| D6 | reserve prelude ctor/type names | **Yes** (§2.2 already mandates it) | slightly stricter; code shadowing `None` was already broken |
| D7 | `split(s,"")` | **Define as `chars(s)`** | panic (R009) is defensible but a defined result is friendlier / Go-precedented |
| D8 | numeric underscores | **Strict L006** | keeping leniency freezes `1_` as valid syntax forever |
| D9 | inf/nan rendering | **Document as-is in Appendix B** | inventing literals for values with no source syntax adds vocabulary for zero benefit |
| D10 | §3.2 arg-flow (W11) | **Fix in v1** | it's a documented-contract violation; the Option story is unwritable without it |
| D11 | cross-module var | **Export mutability + E0103** (W13 shape) | filtering exports is 1 line but reports "no such member" for a visible member |

---

## Definition of done

- **No raw tracebacks:** every audit repro — 100-deep nesting, `not `×400, Latin-1 file, binary stdin, `split("abc","")`, `float(10**500)`, `trunc(inf)`, 5000-deep print, infinite recursion, Ctrl-C — produces a Weft diagnostic/panic with the §6.2 exit code, and a test asserts each incl. `"Traceback" not in output`.
- **Soundness closed:** TS-BLOCKER-1 repro is a check-time error; no checker-clean program in the suite raises a host `TypeError`; foreign-enum match is a clean E-code.
- **REPL honors §6.4:** multi-statement fn/match/if-else enterable, blank-line rule, comment-safe, Ctrl-C-safe — each locked by a `test_repl.py` test.
- **Leaks bounded:** select waiters removed on wake, DONE fibers pruned, recursion panics at the cap — each with a white-box test.
- **Spec == implementation:** DESIGN v1.2 parses select + `[T,...]`; §2.6 and §2.8 verbatim examples run as goldens; Appendix C lists every code in `errors.py`; DECISIONS.md has the v1.2 row.
- **Release engineering:** `LICENSE` present; CI green on `main` (full suite + all 12 examples); `pip install .` gives a working `weft` command; version single-sourced; `v0.1.0` tag + CHANGELOG; README count non-stale; prototypes un-commit-able; cli.py coverage >90%, overall ≥ 91% baseline.

**Rough total:** ~6 S, ~9 M, 1 M/L — on the order of 3–4 focused weekends. The release gate (Phases 0–3) is reachable in the first ~1.5.
