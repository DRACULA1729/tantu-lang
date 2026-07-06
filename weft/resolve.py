"""Name resolution: the Resolution union (compiler-facing) + the scope machinery (checker-facing).

DESIGN.md §3.5 + Fable ruling: `Name.res` is a frozen Resolution the compiler dispatches on.
Storage model: a *frame root* (the module now; a function in M3) owns a monotonic slot
counter; *block scopes* (if/while/loop bodies) add name visibility only and allocate from the
nearest enclosing root. Slots are monotonic and NEVER reused — sibling blocks and shadows get
distinct slots, so re-entering a loop body simply re-stores the same slot. In M2 every binding
is a Global.

The compiler imports only the Resolution names (top of this file); it never touches the Scope
internals below — keeping the "one home per cross-stage type" invariant (§6.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from weft.errors import Span
from weft.wtypes import Type


# --------------------------------------------------------------- Resolution (compiler-facing)

@dataclass(frozen=True, slots=True)
class Global:
    index: int


@dataclass(frozen=True, slots=True)
class Local:
    slot: int


@dataclass(frozen=True, slots=True)
class Capture:      # M3
    index: int


@dataclass(frozen=True, slots=True)
class Builtin:
    name: str


@dataclass(frozen=True, slots=True)
class VariantRes:      # M4: a name that constructs an enum variant
    enum_name: str
    variant_name: str
    tag: int
    arity: int


@dataclass(frozen=True, slots=True)
class StructRes:       # M5: a name that constructs a struct record
    struct_name: str
    arity: int


@dataclass(frozen=True, slots=True)
class ModuleRef:       # M8: a name bound by `import name` (not a value)
    module: str


@dataclass(frozen=True, slots=True)
class StructField:     # M8: Field.res — struct field access at an index
    index: int


@dataclass(frozen=True, slots=True)
class ImportedGlobal:  # M8: Field.res — a cross-module global (module + local slot)
    module: str
    slot: int


@dataclass(frozen=True, slots=True)
class ErrorRes:
    """Benign placeholder for an unresolved name so nothing downstream crashes."""


ERROR_RES = ErrorRes()

Resolution = Global | Local | Capture | Builtin | VariantRes | StructRes | ErrorRes


# ------------------------------------------------------------------ scope machinery (checker)

class FrameRoot:
    """Owns a monotonic slot counter for one frame (the module, or a function in M3).

    Function roots also accumulate a capture list (closure conversion, Fable M3 ruling 2):
    `captures[i]` is the Resolution — as seen by the *enclosing* frame — that supplies capture
    slot `i` at MAKE_CLOSURE time.
    """

    def __init__(self, kind: str, parent: "FrameRoot | None" = None) -> None:
        self.kind = kind                        # "global" | "local"
        self.parent = parent                    # enclosing frame root (None for the module)
        self.counter = 0
        self.captures: list[Resolution] = []
        self._capture_index: dict[int, int] = {}

    def alloc(self) -> int:
        slot = self.counter
        self.counter += 1
        return slot


@dataclass
class BindingInfo:
    name: str
    ty: Type
    mutable: bool
    root: FrameRoot     # the frame that owns this binding's slot
    slot: int
    decl_span: Span
    generic: bool = False   # M9b: a top-level generic fn (ty is an FnT containing TypeVars)

    def resolution(self) -> Resolution:
        return Global(self.slot) if self.root.kind == "global" else Local(self.slot)


def resolve(current: FrameRoot, binding: BindingInfo) -> Resolution:
    """How `current`'s frame reaches `binding`: direct Local/Global, or a threaded Capture."""
    owner = binding.root
    if owner is current or owner.kind == "global":
        return binding.resolution()             # same frame, or a module global (reachable anywhere)
    return Capture(_capture_in(current, binding))   # a `let` in an enclosing function


def _capture_in(root: FrameRoot, binding: BindingInfo) -> int:
    """Register `binding` as a capture of function `root`, threading through intermediates."""
    key = id(binding)
    if key in root._capture_index:
        return root._capture_index[key]
    parent = root.parent
    assert parent is not None, "capture with no enclosing frame"
    if binding.root is parent:
        source: Resolution = binding.resolution()          # parent owns it (a Local)
    else:
        source = Capture(_capture_in(parent, binding))       # parent must capture it too
    idx = len(root.captures)
    root.captures.append(source)
    root._capture_index[key] = idx
    return idx


class Scope:
    """A lexical scope: a name→BindingInfo map, a parent link, and its owning frame root.

    Block scopes share their function/module root; a function body starts a fresh root whose
    scope's parent is still the enclosing lexical scope (so lookups cross frames, and `resolve`
    classifies the result as Local/Global/Capture).
    """

    def __init__(self, root: FrameRoot, parent: "Scope | None" = None, *, repl: bool = False) -> None:
        self.root = root
        self.parent = parent
        self.repl = repl            # REPL module scope: re-let redefines instead of erroring
        self.names: dict[str, BindingInfo] = {}

    def child(self) -> "Scope":
        return Scope(self.root, parent=self)

    def child_frame(self, kind: str = "local") -> "Scope":
        """A scope for a new function body: a fresh root, still lexically parented here."""
        return Scope(FrameRoot(kind, parent=self.root), parent=self)

    def declare(self, name: str, ty: Type, mutable: bool, decl_span: Span,
                *, generic: bool = False) -> BindingInfo:
        info = BindingInfo(name, ty, mutable, self.root, self.root.alloc(), decl_span,
                           generic=generic)
        self.names[name] = info
        return info

    def declare_at(self, name: str, ty: Type, slot: int, decl_span: Span) -> BindingInfo:
        """Declare an immutable binding at an already-allocated slot (match pattern bindings)."""
        info = BindingInfo(name, ty, False, self.root, slot, decl_span)
        self.names[name] = info
        return info

    def local(self, name: str) -> BindingInfo | None:
        """Look up a binding declared directly in THIS scope (for duplicate detection)."""
        return self.names.get(name)

    def lookup(self, name: str) -> BindingInfo | None:
        """Look up a name in this scope and its ancestors (crossing frame boundaries)."""
        scope: Scope | None = self
        while scope is not None:
            info = scope.names.get(name)
            if info is not None:
                return info
            scope = scope.parent
        return None
