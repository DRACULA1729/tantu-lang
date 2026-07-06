"""Interactive REPL (DESIGN.md §6.4).

A `_Session` holds a persistent module scope and a growing global table, so bindings survive
across inputs (Fable ruling 1b). Each input is checked in a fresh child scope over the
session root and only committed to the session on success — a failed input never corrupts
session state. A trailing bare expression is echoed. A line ending in ':' or with unclosed
brackets continues until a blank line. Errors render inline; Ctrl-D (EOF) exits 0. Driven
with (stdin, stdout) so tests can supply StringIO.
"""

from __future__ import annotations

from weft import __version__
from weft.lexer import tokenize
from weft.parser import parse
from weft.checker import check
from weft.compiler import compile_module
from weft.vm import VM
from weft.resolve import Scope, FrameRoot
from weft.errors import WeftError, CheckErrors, render_error, render_errors


class _Session:
    def __init__(self, stdout) -> None:
        self.stdout = stdout
        self.root = FrameRoot("global")
        self.scope = Scope(self.root, repl=True)   # persistent module scope
        self.globals: list = []                     # persistent global values
        self.vm = VM(stdout=stdout)

    def run_input(self, src: str) -> None:
        module = parse(tokenize(src, "<repl>"))
        entry = self.scope.child()
        entry.repl = True
        check(module, module_scope=entry)           # may raise; self.scope untouched on error
        code = compile_module(module, repl=True)
        self.scope.names.update(entry.names)         # commit new/redefined bindings
        self.vm.run(code, src, "<repl>", globals=self.globals)


def _needs_continuation(buf: str) -> bool:
    if buf.rstrip().endswith(":"):
        return True
    depth = 0
    in_str = False
    esc = False
    for ch in buf:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
    return depth > 0


def repl(stdin, stdout) -> int:
    stdout.write(f"weft {__version__} REPL — Ctrl-D to exit\n")
    session = _Session(stdout)
    while True:
        try:
            stdout.write(">>> ")
            stdout.flush()
            line = stdin.readline()
            if line == "":  # EOF
                stdout.write("\n")
                return 0
            buf = line
            while _needs_continuation(buf):
                stdout.write("... ")
                stdout.flush()
                cont = stdin.readline()
                if cont == "":
                    break
                buf += cont
            if buf.strip() == "":
                continue
            try:
                session.run_input(buf)
            except CheckErrors as e:
                stdout.write(render_errors(buf, e.errors) + "\n")
            except WeftError as e:
                stdout.write(render_error(buf, e) + "\n")
        except KeyboardInterrupt:           # Ctrl-C: abandon the current line, keep the session
            stdout.write("\nKeyboardInterrupt\n")
            continue
        except Exception as e:              # catch-all boundary: the session must never die (W1)
            stdout.write(f"weft: internal error: {type(e).__name__}: {e}\n"
                         "  this is a bug in weft — please report it\n")
            continue
