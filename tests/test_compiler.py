"""Compiler unit tests (DESIGN.md §8.1): dis() goldens lock the ISA + lowering shape."""

from __future__ import annotations

import pytest

from tantu.bytecode import Op
from tantu.cli import compile_source
from harness import GOLDEN, check_or_update

DIS_SNIPPETS = {
    "arith": "print(1 + 2 * 3)\n",
    "unary_mod": "print(-7 % 3)\n",
    "multi": "print(1)\nprint(2)\n",
    # M2 — lock the control-flow + short-circuit lowering shapes
    "while_loop": "var i = 0\nwhile i < 3:\n    i = i + 1\n",
    "if_stmt": "if true:\n    print(1)\nelse:\n    print(2)\n",
    "and_or": "let b = true and false\n",
    # M3 — lock the closure / call / return + main-epilogue shapes
    "function": "fn dbl(n: Int) -> Int:\n    n + n\nfn main() -> Unit:\n    print(dbl(21))\n",
    # M4 — lock variant construction + match compilation (TEST/UNPACK_VARIANT, MATCH_FAIL)
    "match": ("enum Opt:\n    S(Int)\n    N\nfn get(o: Opt) -> Int:\n"
              "    match o:\n        S(x) => x\n        N => 0\n"),
    # M5 — lock the for-loop desugar (range/len builtin call, INDEX, increment)
    "for_loop": "fn main() -> Unit:\n    for x in range(0, 3):\n        print(x)\n",
    # M7 — lock channel intrinsics → opcodes + spawn
    "channels": ("fn w(c: Chan[Int]) -> Unit:\n    send(c, 1)\n"
                 "fn main() -> Unit:\n    let c = chan[Int](4)\n    spawn w(c)\n    print(recv(c))\n"),
    # M9a — lock select lowering: operand pushes, SELECT (has_else, kinds), SELECT_DISPATCH table,
    # per-arm STORE/POP prologue, JUMP-to-end after every arm but the last
    "select": ("fn main() -> Unit:\n    let a = chan[Int]()\n    let b = chan[Int]()\n"
               "    select:\n        v = recv(a) =>\n            print(v)\n"
               "        recv(b) =>\n            print(\"b\")\n        else =>\n            print(\"none\")\n"),
    # M9b — the erasure lock: a generic fn compiles to ONE Code, byte-identical to a monomorphic
    # equivalent, and both differently-typed call sites emit the same CALL (no specialization)
    "generic_erasure": ("fn id[T](x: T) -> T:\n    x\n"
                        "fn main() -> Unit:\n    print(id(42))\n    print(id(\"hi\"))\n"),
}


@pytest.mark.parametrize("name", sorted(DIS_SNIPPETS))
def test_dis_golden(name, update_goldens):
    code = compile_source(DIS_SNIPPETS[name], f"{name}.tn")
    check_or_update(GOLDEN / "dis" / f"{name}.txt", code.dis() + "\n", update_goldens)


def test_module_ends_with_halt_and_spans_are_parallel():
    code = compile_source("print(1)\n", "t.tn")
    assert code.instrs[-1].op is Op.HALT
    assert len(code.instrs) == len(code.spans)


def test_print_lowers_to_print_then_pop():
    # print(x) => LOAD_CONST; PRINT; POP; HALT  (PRINT pushes Unit, POP discards it)
    ops = [i.op for i in compile_source("print(1)\n", "t.tn").instrs]
    assert ops == [Op.LOAD_CONST, Op.PRINT, Op.POP, Op.HALT]
