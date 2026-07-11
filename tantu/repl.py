"""Interactive REPL (DESIGN.md §6.4).

A `_Session` holds a persistent module scope and a growing global table, so bindings survive
across inputs (Fable ruling 1b). Each input is checked in a fresh child scope over the
session root and only committed to the session on success — a failed input never corrupts
session state. A trailing bare expression is echoed.

Multi-line rule (§6.4): the decision to ENTER continuation is made once, on the FIRST
line — a (comment-stripped) line ending in ':' or a line with unclosed brackets. Once in
continuation mode, lines are read until a blank line (or EOF) submits the whole buffer.
Errors render inline; Ctrl-D (EOF) exits 0. Driven with (stdin, stdout) so tests can
supply StringIO.
"""

from __future__ import annotations

from tantu import __version__
from tantu.lexer import tokenize
from tantu.parser import parse
from tantu.checker import check
from tantu.compiler import compile_module
from tantu.vm import VM
from tantu.resolve import Scope, FrameRoot
from tantu.errors import TantuError, CheckErrors, render_error, render_errors


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


def _strip_comment(line: str) -> str:
    """Cut a trailing '# ...' comment, string-aware: a '#' inside a string literal is not
    a comment. Mirrors the lexer's string rules (lexer._string / _skip_comment): strings
    are double-quoted, single-line, with backslash escapes."""
    in_str = False
    esc = False
    for i, ch in enumerate(line):
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
        elif ch == "#":
            return line[:i]
    return line


def _opens_block(text: str) -> bool:
    """A block header: a (comment-stripped) line ending in ':' — fn/if/match/for/while/
    enum/struct/select etc."""
    return text.rstrip().endswith(":")


def _open_brackets(buf: str) -> bool:
    """True if buf has more unclosed (/[/{ than closers, ignoring brackets inside string
    literals and comments (same string rules as _strip_comment; strings and comments both
    end at a newline, per the lexer)."""
    depth = 0
    in_str = False
    esc = False
    in_comment = False
    for ch in buf:
        if in_comment:
            if ch == "\n":
                in_comment = False
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"' or ch == "\n":  # strings are single-line in v1
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "#":
            in_comment = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
    return depth > 0


def repl(stdin, stdout) -> int:
    stdout.write(f"tantu {__version__} REPL — Ctrl-D to exit\n")
    session = _Session(stdout)
    while True:
        try:
            stdout.write(">>> ")
            stdout.flush()
            line = stdin.readline()
            if line == "":  # EOF
                stdout.write("\n")
                return 0
            if line.startswith("﻿"):   # strip a pasted leading BOM
                line = line[1:]
            line = line.replace("\r\n", "\n").replace("\r", "\n")   # CRLF/CR → LF (piped scripts)
            buf = line
            # §6.4: the continuation decision is made ONCE, on the first line. In
            # continuation mode a blank line (or EOF) submits the whole buffer.
            if _opens_block(_strip_comment(line)) or _open_brackets(buf):
                while True:
                    stdout.write("... ")
                    stdout.flush()
                    cont = stdin.readline()
                    if cont == "" or cont.strip() == "":
                        break
                    buf += cont.replace("\r\n", "\n").replace("\r", "\n")   # CRLF/CR → LF
            if buf.strip() == "":
                continue
            try:
                session.run_input(buf)
            except CheckErrors as e:
                stdout.write(render_errors(buf, e.errors) + "\n")
            except TantuError as e:
                stdout.write(render_error(buf, e) + "\n")
        except KeyboardInterrupt:           # Ctrl-C: abandon the current line, keep the session
            stdout.write("\nKeyboardInterrupt\n")
            continue
        except BrokenPipeError:             # stdout consumer went away: exit the loop, don't busy-spin
            return 141
        except Exception as e:              # catch-all boundary: the session must never die (W1)
            stdout.write(f"tantu: internal error: {type(e).__name__}: {e}\n"
                         "  this is a bug in tantu — please report it\n")
            continue
