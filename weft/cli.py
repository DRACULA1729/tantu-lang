"""Command-line driver: wires the pipeline and renders errors (DESIGN.md §6.1).

Subcommands: `run <file|->`, `check <file|->`, `dis <file|->`; no subcommand → REPL;
`--version`. Exit codes (DESIGN.md §6.2): 0 success, 1 panic, 2 compile-time or usage error.
Streams: program output + panic trace → stdout; compile errors → stderr (DECISIONS.md).

The pipeline helpers (`front`, `compile_source`, `run_text`, ...) are importable so the
in-process golden runner (tests/conftest.py) can drive them without a subprocess.
"""

from __future__ import annotations

import os
import signal
import sys

from weft import __version__
from weft.lexer import tokenize
from weft.parser import parse
from weft.checker import check
from weft.compiler import compile_module
from weft.bytecode import Code
from weft import ast_nodes as A
from weft.vm import VM
from weft.linker import link, CompiledModule
from weft.errors import (
    WeftError, CheckErrors, render_error, render_errors, E_IMPORT_CYCLE, E_MODULE_NOT_FOUND,
)

_USAGE = ("usage: weft [run|check|dis] <file|-> \n"
          "       weft            # REPL\n"
          "       weft --version")


def _internal_error(err, exc) -> int:
    """Last-resort boundary (§6.2): any unexpected exception becomes a one-line
    diagnostic and exit 2 — a raw Python traceback must never reach the user."""
    err.write(f"weft: internal error: {type(exc).__name__}: {exc}\n"
              "  this is a bug in weft — please report it\n")
    return 2


def _silence(stream) -> None:
    """Redirect a stream's fd to /dev/null so a later flush cannot re-raise on a broken
    pipe. Guarded: StringIO.fileno() raises UnsupportedOperation (an OSError subclass) for
    the in-process golden runner — swallow it so tests stay unaffected."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, stream.fileno())
    except OSError:
        pass


def front(src: str, file: str) -> A.Module:
    """Lex + parse + check. Raises WeftError / CheckErrors on failure."""
    return check(parse(tokenize(src, file)))


def compile_source(src: str, file: str, *, repl: bool = False) -> Code:
    return compile_module(front(src, file), repl=repl)


def _emit_compile_error(exc: Exception, src: str, err) -> int:
    if isinstance(exc, CheckErrors):
        err.write(render_errors(src, exc.errors) + "\n")
    else:
        assert isinstance(exc, WeftError)
        err.write(render_error(src, exc) + "\n")
    return 2


def run_text(src: str, file: str, out, err) -> int:
    try:
        code = compile_source(src, file)
    except (WeftError, CheckErrors) as e:
        return _emit_compile_error(e, src, err)
    return VM(stdout=out).run(code, src, file)


def check_text(src: str, file: str, err) -> int:
    try:
        front(src, file)
    except (WeftError, CheckErrors) as e:
        return _emit_compile_error(e, src, err)
    return 0


def dis_text(src: str, file: str, out, err) -> int:
    try:
        code = compile_source(src, file)
    except (WeftError, CheckErrors) as e:
        return _emit_compile_error(e, src, err)
    out.write(code.dis() + "\n")
    return 0


def build_program(root_path: str, err) -> tuple[Code, dict] | None:
    """Discover, check (topological order), compile, and link a multi-module program.

    Returns (entry_code, srcs) where srcs maps file-label→source, or None after writing a
    rendered error to `err`. Modules are sibling `name.weft` files in the root's directory;
    import cycles and missing modules are compile errors (DESIGN.md §7 M8).
    """
    root_abs = os.path.abspath(root_path)
    root_dir = os.path.dirname(root_abs)
    root_name = os.path.splitext(os.path.basename(root_abs))[0]
    order: list[str] = []
    cache: dict[str, tuple] = {}
    state: dict[str, str] = {}
    srcs: dict[str, str] = {}

    def visit(name: str, path: str, label: str, stack: list[str]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            raise WeftError(E_IMPORT_CYCLE, "import cycle: " + " -> ".join(stack + [label]),
                            None, stage="check")
        state[name] = "visiting"
        with open(path, "r", encoding="utf-8-sig") as f:   # -sig: drop a leading BOM
            src = f.read()
        srcs[label] = src
        ast = parse(tokenize(src, label))
        imports = [d for d in ast.decls if isinstance(d, A.Import)]
        for imp in imports:
            ipath = os.path.join(root_dir, imp.name + ".weft")
            if not os.path.exists(ipath):
                raise WeftError(E_MODULE_NOT_FOUND,
                                f"cannot find module '{imp.name}' ({imp.name}.weft)",
                                imp.span, stage="check")
            visit(imp.name, os.path.abspath(ipath), imp.name + ".weft", stack + [label])
        state[name] = "done"
        cache[name] = (label, ast, [d.name for d in imports])
        order.append(name)

    try:
        visit(root_name, root_abs, os.path.basename(root_abs), [])
        symbols: dict[str, dict] = {}
        compiled: list[CompiledModule] = []
        for name in order:                              # topological: deps first, root last
            label, ast, imps = cache[name]
            check(ast, imports={imp: symbols[imp] for imp in imps})
            symbols[name] = ast.symbols
            is_root = name == root_name
            code = compile_module(ast, is_root=is_root,
                                  name="<module>" if is_root else f"<module:{name}>")
            compiled.append(CompiledModule(name, code, ast.n_globals))
        return link(compiled), srcs
    except CheckErrors as e:
        src = srcs.get(e.errors[0].span.file, "") if e.errors and e.errors[0].span else ""
        err.write(render_errors(src, e.errors) + "\n")
        return None
    except WeftError as e:
        src = srcs.get(e.span.file, "") if e.span else ""
        err.write(render_error(src, e) + "\n")
        return None
    except UnicodeDecodeError as e:
        err.write(f"weft: {root_path}: source is not valid UTF-8 ({e})\n")
        return None
    except OSError as e:
        err.write(f"weft: cannot read {root_path}: {e.strerror}\n")
        return None


def run_file(path: str, out, err) -> int:
    built = build_program(path, err)
    if built is None:
        return 2
    entry, srcs = built
    return VM(stdout=out).run(entry, srcs=srcs)


def check_file(path: str, err) -> int:
    return 0 if build_program(path, err) is not None else 2


def dis_file(path: str, out, err) -> int:
    built = build_program(path, err)
    if built is None:
        return 2
    entry, _ = built
    out.write(entry.dis() + "\n")
    return 0


def _read_target(target: str, err) -> tuple[str, str] | None:
    try:
        if target == "-":
            src = sys.stdin.read()
            if src.startswith("﻿"):    # strip a leading BOM (files use utf-8-sig)
                src = src[1:]
            src = src.replace("\r\n", "\n").replace("\r", "\n")  # universal newlines, matching open()
            return src, "<stdin>"
        with open(target, "r", encoding="utf-8-sig") as f:   # -sig: drop a leading BOM
            return f.read(), target
    except UnicodeDecodeError as e:
        err.write(f"weft: {target}: source is not valid UTF-8 ({e})\n")
        return None
    except OSError as e:
        err.write(f"weft: cannot read {target}: {e.strerror}\n")
        return None


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 12):
        sys.stderr.write("weft requires Python 3.12 or newer\n")
        return 2

    # Deep-but-bounded recursive AST walks (a long flat operator chain nests into a
    # left-deep BinOp list the checker/compiler recurse over) can exceed CPython's
    # default ~1000-frame limit; 3.12's heap-allocated frames make a high limit cheap.
    # The parser caps a single chain (_MAX_CHAIN) well inside this so it never triggers.
    sys.setrecursionlimit(100_000)

    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        if not argv:                        # REPL: its own loop handles pipes/Ctrl-C; the flush
            from weft.repl import repl       # below routes any still-buffered output through this
            rc = repl(sys.stdin, sys.stdout)  # boundary so a broken pipe can't leak at shutdown
            sys.stdout.flush()
            sys.stderr.flush()
            return rc

        if argv[0] in ("--version", "-V"):
            sys.stdout.write(f"weft {__version__}\n")
            sys.stdout.flush()   # flush inside the boundary so a broken pipe surfaces here, not at shutdown
            return 0

        cmd = argv[0]
        if cmd not in ("run", "check", "dis"):
            sys.stderr.write(f"weft: unknown command {cmd!r}\n{_USAGE}\n")
            return 2
        if len(argv) < 2:
            sys.stderr.write(f"weft {cmd}: missing file argument (use '-' for stdin)\n")
            return 2
        target = argv[1]

        if target != "-":                   # a source file: multi-module (discover + link)
            if cmd == "run":
                rc = run_file(target, sys.stdout, sys.stderr)
            elif cmd == "check":
                rc = check_file(target, sys.stderr)
            else:
                rc = dis_file(target, sys.stdout, sys.stderr)
        else:
            read = _read_target("-", sys.stderr)  # stdin: single module (import is a compile error)
            if read is None:
                return 2
            src, label = read
            if cmd == "run":
                rc = run_text(src, label, sys.stdout, sys.stderr)
            elif cmd == "check":
                rc = check_text(src, label, sys.stderr)
            else:
                rc = dis_text(src, label, sys.stdout, sys.stderr)
        sys.stdout.flush()   # surface a pending broken pipe HERE (inside the boundary), not at
        sys.stderr.flush()   # interpreter shutdown where it leaks as "Exception ignored ..."
        return rc
    except SystemExit:
        raise
    except KeyboardInterrupt:               # Ctrl-C during a command: clean exit, no traceback
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a 2nd Ctrl-C during teardown must not crash
        try:
            sys.stderr.write("\nweft: interrupted\n")
        except (BrokenPipeError, OSError):  # stderr may itself be a broken pipe
            pass
        for stream in (sys.stdout, sys.stderr):   # a broken-pipe interrupt still exits 130, not 141
            try:
                stream.flush()
            except (BrokenPipeError, OSError):
                _silence(stream)
        return 130
    except BrokenPipeError:                 # an output consumer went away (e.g. `weft run x | head`):
        # a normal end-of-output, not a weft bug. Redirect BOTH streams to /dev/null so the shutdown
        # flush cannot raise again, write nothing, and exit 141 (128 + SIGPIPE) per the shell convention.
        _silence(sys.stdout)
        _silence(sys.stderr)
        return 141
    except Exception as exc:                # catch-all boundary: no raw tracebacks (W1)
        return _internal_error(sys.stderr, exc)
