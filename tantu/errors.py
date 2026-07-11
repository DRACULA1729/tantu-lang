"""Spans, the TantuError hierarchy, and the single caret-rendering function.

Everything the compiler and VM report to the user flows through here (DESIGN.md §6.2:
"rendered by ONE function"). Source offsets are the single source of truth for a Span;
line/col are carried for display but the renderer re-derives the column from offsets so a
Span can never disagree with the text it points at.

Error-code registry: DESIGN.md Appendix C. Codes are append-only and never reused.
Stages: L0xx lex, P0xx parse, E0xxx check, R0xx runtime panic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Span:
    """A half-open source range [start, end) plus the line/col of `start` (for display).

    `start`/`end` are 0-based character offsets into the source string. `line` is 1-based,
    `col` is 0-based. `file` is a display label only ("<stdin>", "<repl>", or a path).
    Synthetic/EOF tokens use a zero-width span at len(src) (see `Span.eof`).
    """

    file: str
    start: int
    end: int
    line: int
    col: int

    @staticmethod
    def merge(a: "Span", b: "Span") -> "Span":
        """Span covering a.start .. b.end, keeping a's line/col (a starts first)."""
        return Span(a.file, a.start, b.end, a.line, a.col)

    @staticmethod
    def eof(file: str, src: str) -> "Span":
        """A zero-width span pointing just past the last character of `src`."""
        line = src.count("\n") + 1
        last_nl = src.rfind("\n")
        col = len(src) - (last_nl + 1)
        return Span(file, len(src), len(src), line, col)


# --------------------------------------------------------------------------- errors

class TantuError(Exception):
    """A user-facing compile-time error. One of lex / parse / check.

    Carries a stable code (Appendix C), a message, the offending span, and an optional hint.
    """

    def __init__(self, code: str, message: str, span: Span | None,
                 *, stage: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span
        self.stage = stage
        self.hint = hint


class LexError(TantuError):
    def __init__(self, code: str, message: str, span: Span | None, *, hint: str | None = None) -> None:
        super().__init__(code, message, span, stage="lex", hint=hint)


class ParseError(TantuError):
    def __init__(self, code: str, message: str, span: Span | None, *, hint: str | None = None) -> None:
        super().__init__(code, message, span, stage="parse", hint=hint)


class CheckError(TantuError):
    def __init__(self, code: str, message: str, span: Span | None, *, hint: str | None = None) -> None:
        super().__init__(code, message, span, stage="check", hint=hint)


class CheckErrors(Exception):
    """A batch of check-stage errors reported together (DESIGN.md §3.1: not first-error-only)."""

    def __init__(self, errors: list[TantuError]) -> None:
        super().__init__(f"{len(errors)} error(s)")
        self.errors = errors


class TantuPanic(Exception):
    """A runtime panic (DESIGN.md §2.8). Terminates the program with exit code 1.

    The VM attaches the fiber's frame trace when it catches this in run().
    """

    def __init__(self, code: str, message: str, span: Span | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.span = span


# ------------------------------------------------------------------- rendering

def _line_bounds(src: str, offset: int) -> tuple[int, int]:
    """Return (line_start, line_end) offsets of the physical line containing `offset`."""
    line_start = src.rfind("\n", 0, offset) + 1  # rfind→-1 gives 0 for the first line
    line_end = src.find("\n", offset)
    if line_end == -1:
        line_end = len(src)
    return line_start, line_end


def render_span(src: str, span: Span) -> list[str]:
    """The source line for `span` plus a caret line beneath it (both indented two spaces).

    Single line only: a multi-line span underlines from its start to the end of its first
    line, never across lines (DESIGN.md §1 ruling — blocks point at their header line).
    Leading whitespace is copied verbatim into the caret pad so carets stay aligned.
    """
    line_start, line_end = _line_bounds(src, min(span.start, len(src)))
    source_line = src[line_start:line_end]
    col = span.start - line_start  # derived from offsets — the source of truth

    caret_end = min(span.end, line_end)
    caret_len = max(1, caret_end - span.start)

    pad_chars = []
    for i in range(col):
        ch = source_line[i] if i < len(source_line) else " "
        pad_chars.append("\t" if ch == "\t" else " ")
    caret_line = "".join(pad_chars) + ("^" * caret_len)

    return [f"  {source_line}", f"  {caret_line}"]


def render_error(src: str, err: TantuError) -> str:
    """Render one compile-time error: a header line, the source line, and a caret."""
    lines: list[str] = []
    if err.span is not None:
        s = err.span
        loc = f"{s.file}:{s.line}:{s.col + 1}"
        lines.append(f"{loc}: {err.stage} error [{err.code}]: {err.message}")
        lines.extend(render_span(src, s))
    else:
        lines.append(f"{err.stage} error [{err.code}]: {err.message}")
    if err.hint:
        lines.append(f"  hint: {err.hint}")
    return "\n".join(lines)


def render_errors(src: str, errs: list[TantuError]) -> str:
    """Render a batch of errors (blank-line separated)."""
    return "\n\n".join(render_error(src, e) for e in errs)


def render_panic(srcs: dict, trace: list[tuple[str, Span | None]], panic: TantuPanic) -> str:
    """Render a runtime panic + fiber stack trace (innermost first). `srcs` maps file→source
    so cross-module frames caret against their own file."""
    lines = [f"panic [{panic.code}]: {panic.message}"]
    for name, span in trace:
        if span is not None:
            lines.append(f"  in {name} at {span.file}:{span.line}:{span.col + 1}")
            src = srcs.get(span.file, "")
            if src:
                lines.extend(render_span(src, span))
        else:
            lines.append(f"  in {name}")
    return "\n".join(lines)


# --------------------------------------------------------------- code constants (App. C)

# Lex
L_TABS_IN_INDENT = "L001"
L_BAD_DEDENT = "L002"
L_ILLEGAL_CHAR = "L003"
L_INVALID_ESCAPE = "L004"
L_UNTERMINATED_STRING = "L005"
L_INVALID_NUMBER = "L006"       # malformed numeric literal (misplaced underscore)
# Parse
P_UNEXPECTED_TOKEN = "P001"
P_UNEXPECTED_EOF = "P002"
P_TOO_DEEP = "P003"             # expression/type/pattern nesting too deep
# Check
E_UNSUPPORTED = "E0001"   # construct not yet available in the current build milestone
E_DUPLICATE = "E0101"
E_ASSIGN_IMMUTABLE = "E0102"
E_VAR_CAPTURE = "E0103"
E_NON_BOOL_COND = "E0104"
E_QUESTION = "E0106"
E_MISSING_RETURN = "E0108"
E_INTRINSIC_NOT_VALUE = "E0109"
E_UNKNOWN_NAME = "E0111"
E_TYPE_MISMATCH = "E0112"
E_NOT_COMPARABLE = "E0113"
E_LOOP_CONTROL = "E0114"
E_EMPTY_LIST = "E0107"
E_UNKNOWN_TYPE = "E0115"
E_NON_EXHAUSTIVE = "E0105"
E_RETURN_OUTSIDE = "E0116"
E_NOT_CALLABLE = "E0117"
E_ARITY = "E0118"
E_CANNOT_INFER = "E0119"
E_UNREACHABLE = "E0120"
E_NOT_A_VARIANT = "E0121"
E_VARIANT_ARITY = "E0122"
E_CONSTRUCTOR_NOT_VALUE = "E0123"
E_DUP_VARIANT = "E0124"
E_IMPORT_CYCLE = "E0130"
E_MODULE_NOT_FOUND = "E0131"
E_NOT_A_MODULE_MEMBER = "E0132"
E_MODULE_NOT_VALUE = "E0133"
E_IMPORT_UNSUPPORTED = "E0134"
E_SELECT_ELSE = "E0135"
E_GENERIC_NOT_VALUE = "E0136"
# Runtime
R_DIV_BY_ZERO = "R001"
R_INDEX_OUT_OF_RANGE = "R002"
R_SEND_CLOSED = "R003"
R_CLOSE_CLOSED = "R004"
R_DEADLOCK = "R005"
R_UNINIT = "R006"
R_MATCH_FAIL = "R007"
R_STEP_LIMIT = "R008"
R_BUILTIN = "R009"              # builtin domain error (wrapped host exception)
R_STACK_OVERFLOW = "R010"      # call stack overflow ({cap} frames)
R_BAD_CAP = "R011"             # channel capacity cannot be negative ({cap})
