"""Link-time global flattening for multi-module programs (DESIGN.md §7 M8, Fable ruling).

Each module compiles independently with its own 0-based global slots; the linker assigns each
module a base offset into one combined global table and rewrites every LOAD_GLOBAL/STORE_GLOBAL
argument by that base — so the VM and ISA stay exactly as M1–M7 shipped (single-operand
LOAD_GLOBAL over a flat `self.globals`). Cross-module references are emitted by the compiler as
a `CrossRef(module, slot)` marker in the instruction arg and resolved here to `base[module] +
slot`. A synthetic `<entry>` runs each imported module's init (topological order, deps first)
then the root module.
"""

from __future__ import annotations

from dataclasses import dataclass

from weft.bytecode import Op, Instr, Code
from weft.errors import Span


@dataclass(frozen=True, slots=True)
class CrossRef:
    """A LOAD_GLOBAL/STORE_GLOBAL arg naming another module's global; flattened at link time."""
    module: str
    slot: int


@dataclass
class CompiledModule:
    name: str
    code: Code          # <module> (root, ends in HALT) or <module:name> init (ends in RETURN)
    n_globals: int


_GLOBAL_OPS = (Op.LOAD_GLOBAL, Op.STORE_GLOBAL)


def _flatten(code: Code, own_base: int, bases: dict[str, int]) -> None:
    """Rewrite global slot args in `code` and its nested function Codes, in place."""
    for i, instr in enumerate(code.instrs):
        if instr.op in _GLOBAL_OPS:
            arg = instr.arg
            if isinstance(arg, CrossRef):
                code.instrs[i] = Instr(instr.op, bases[arg.module] + arg.slot)
            else:
                code.instrs[i] = Instr(instr.op, arg + own_base)
    for c in code.consts:
        if isinstance(c, Code):
            _flatten(c, own_base, bases)


def link(modules: list[CompiledModule]) -> Code:
    """Flatten globals and return the entry Code. `modules` is topo-ordered (deps first, root last)."""
    bases: dict[str, int] = {}
    total = 0
    for m in modules:
        bases[m.name] = total
        total += m.n_globals
    for m in modules:
        _flatten(m.code, bases[m.name], bases)

    root = modules[-1]
    if len(modules) == 1:
        root.code.n_globals = total          # base 0; byte-identical to a single-file program
        return root.code

    zero = Span("<entry>", 0, 0, 1, 0)
    instrs: list[Instr] = []
    consts: list[object] = []
    spans: list[Span] = []

    def emit(op: Op, arg) -> None:
        instrs.append(Instr(op, arg))
        spans.append(zero)

    for m in modules[:-1]:                    # imported module inits, topo order (deps first)
        k = len(consts)
        consts.append(m.code)
        emit(Op.MAKE_CLOSURE, (k, 0))
        emit(Op.CALL, 0)
        emit(Op.POP, None)
    kroot = len(consts)
    consts.append(root.code)                  # root <module> runs top-level + main, then HALTs
    emit(Op.MAKE_CLOSURE, (kroot, 0))
    emit(Op.CALL, 0)
    emit(Op.HALT, None)                       # unreachable (root HALT exits first), for safety
    return Code("<entry>", 0, 0, total, instrs, consts, spans)
