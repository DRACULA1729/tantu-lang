"""M9a `select` tests: lexing, parse shape, type/semantic checks, and runtime behavior.

Runtime cases go through the in-process harness on the virtual clock, so scheduling is
deterministic; blocking/fan-in cases are asserted identical across budgets (max interleaving
at budget=1 vs. the default) per DESIGN.md §8.1.
"""

from __future__ import annotations

import pytest

from weft.lexer import tokenize
from weft.parser import parse
from weft.tokens import TokenType
from weft.errors import (
    CheckErrors, ParseError,
    E_SELECT_ELSE, E_TYPE_MISMATCH, E_INTRINSIC_NOT_VALUE,
)
from weft.cli import front
from harness import run_capture


# --------------------------------------------------------------------- lexing

def test_select_is_a_keyword():
    toks = tokenize("select:\n", "<t>")
    assert toks[0].type is TokenType.SELECT


# --------------------------------------------------------------------- parsing

def _pretty(src: str) -> str:
    return parse(tokenize(src, "<t>")).pretty()


def test_parse_all_arm_forms():
    src = ("select:\n    v = recv(a) => print(v)\n    recv(b) => print(1)\n"
           "    send(c, 2) => print(3)\n    else => print(4)\n")
    p = _pretty(src)
    assert "(recv-arm v (name a)" in p
    assert "(recv-arm _ (name b)" in p           # discard binder renders as _
    assert "(send-arm (name c) (int 2)" in p
    assert "(else-arm " in p


def test_parse_underscore_binder_normalized_to_discard():
    assert "(recv-arm _ (name a)" in _pretty("select:\n    _ = recv(a) => print(1)\n")


def _parse_err(src: str) -> str:
    with pytest.raises(ParseError) as e:
        parse(tokenize(src, "<t>"))
    return e.value.code


def test_parse_missing_fatarrow():
    assert _parse_err("select:\n    recv(a) print(1)\n")


def test_parse_bad_arm_head():
    assert _parse_err("select:\n    foo(a) => print(1)\n")


def test_parse_empty_select_is_error():
    # `select:` with nothing indented can't produce an INDENT suite → parse error
    assert _parse_err("select:\n    \n")


# --------------------------------------------------------------------- checking

def _codes(src: str) -> list:
    with pytest.raises(CheckErrors) as e:
        front(src, "<t>")
    return [x.code for x in e.value.errors]


_WRAP = "fn main() -> Unit:\n    let a = chan[Int]()\n{body}"


def test_recv_binder_types_as_option():
    # Some(x)/None must both be matchable → binder is Option[Int], match is exhaustive
    src = _WRAP.format(body="    select:\n        v = recv(a) =>\n"
                            "            match v:\n                Some(x) => print(x)\n"
                            "                None => print(0)\n")
    front(src, "<t>")   # no error


def test_non_channel_head_is_type_error():
    src = "fn main() -> Unit:\n    let x = 5\n    select:\n        recv(x) => print(1)\n"
    assert E_TYPE_MISMATCH in _codes(src)


def test_send_value_mismatch():
    assert E_TYPE_MISMATCH in _codes(_WRAP.format(body='    select:\n        send(a, "s") => print(1)\n'))


def test_else_must_be_last():
    src = _WRAP.format(body="    select:\n        else => print(0)\n        recv(a) => print(1)\n")
    assert E_SELECT_ELSE in _codes(src)


def test_expression_arms_must_agree():
    src = "fn main() -> Unit:\n    let a = chan[Int]()\n    let x = select:\n" \
          '        recv(a) => 1\n        else => "s"\n    print(x)\n'
    assert E_TYPE_MISMATCH in _codes(src)


def test_statement_position_tolerates_disagreeing_arms():
    # discarded value → arms need not agree
    src = _WRAP.format(body='    select:\n        recv(a) => 1\n        else => "s"\n')
    front(src, "<t>")   # no error


def test_reserved_name_binder():
    assert E_INTRINSIC_NOT_VALUE in _codes(_WRAP.format(body="    select:\n        recv = recv(a) => print(1)\n"))


# --------------------------------------------------------------------- runtime

def _run(src: str, budgets=(1, 1000)) -> str:
    """Run at several budgets, assert identical deterministic output, return it."""
    outs = []
    for b in budgets:
        out, err, code = run_capture(src, "sel.weft", budget=b)
        assert err == "", f"stderr at budget={b}:\n{err}"
        outs.append((out, code))
    assert len(set(outs)) == 1, f"non-deterministic across budgets: {outs}"
    return outs[0][0]


def test_ready_pick_is_source_order():
    src = ("fn main() -> Unit:\n    let a = chan[Int](1)\n    let b = chan[Int](1)\n"
           "    send(a, 10)\n    send(b, 20)\n    select:\n"
           "        v = recv(a) => print(v)\n        w = recv(b) => print(w)\n")
    assert _run(src) == "Some(10)\n"          # both ready → first arm


def test_ready_pick_swapped_order():
    src = ("fn main() -> Unit:\n    let a = chan[Int](1)\n    let b = chan[Int](1)\n"
           "    send(a, 10)\n    send(b, 20)\n    select:\n"
           "        w = recv(b) => print(w)\n        v = recv(a) => print(v)\n")
    assert _run(src) == "Some(20)\n"


def test_else_non_blocking():
    src = ("fn main() -> Unit:\n    let a = chan[Int]()\n    select:\n"
           "        v = recv(a) => print(v)\n        else => print(\"none\")\n")
    assert _run(src) == "none\n"


def test_block_then_woken_by_spawned_sender():
    src = ("fn producer(c: Chan[Int]) -> Unit:\n    send(c, 42)\n\n"
           "fn main() -> Unit:\n    let a = chan[Int]()\n    let b = chan[Int]()\n"
           "    spawn producer(b)\n    select:\n"
           "        v = recv(a) => print(v)\n        w = recv(b) => print(w)\n")
    assert _run(src) == "Some(42)\n"          # a never ready → arm b fires


def test_closed_recv_arm_fires_none():
    src = ("fn main() -> Unit:\n    let a = chan[Int]()\n    close(a)\n    select:\n"
           "        v = recv(a) => print(v)\n        else => print(\"else\")\n")
    assert _run(src) == "None\n"              # closed → recv arm fires with None, not else


def test_closed_send_arm_panics():
    src = ("fn main() -> Unit:\n    let c = chan[Int]()\n    close(c)\n    select:\n"
           "        send(c, 1) => print(\"sent\")\n")
    out, _, code = run_capture(src, "sel.weft")
    assert code == 1 and "R003" in out


def test_close_while_parked_wakes_recv_with_none():
    src = ("fn closer(c: Chan[Int]) -> Unit:\n    close(c)\n\n"
           "fn main() -> Unit:\n    let a = chan[Int]()\n    spawn closer(a)\n    select:\n"
           "        v = recv(a) =>\n            match v:\n"
           "                Some(x) => print(x)\n                None => print(\"closed\")\n")
    assert _run(src) == "closed\n"


def test_deadlock_in_select():
    src = ("fn main() -> Unit:\n    let a = chan[Int]()\n    let b = chan[Int]()\n    select:\n"
           "        v = recv(a) => print(v)\n        w = recv(b) => print(w)\n")
    out, _, code = run_capture(src, "sel.weft")
    assert code == 1 and "R005" in out and "select:" in out    # trace points at the select header


def test_cross_select_rendezvous():
    # a select-send arm rendezvous with a select-recv arm. Only main prints, so the output is
    # interleaving-invariant (which fiber prints "sent" would otherwise race across budgets).
    src = ("fn sender(c: Chan[Int]) -> Unit:\n    var x = 0\n    select:\n"
           "        send(c, 99) => x = 1\n        else => x = 2\n\n"
           "fn main() -> Unit:\n    let c = chan[Int]()\n    spawn sender(c)\n    select:\n"
           "        v = recv(c) => print(v)\n")
    assert _run(src) == "Some(99)\n"


def test_expression_select_binds_result():
    src = ("fn main() -> Unit:\n    let a = chan[Int](1)\n    send(a, 7)\n"
           "    let label = select:\n        v = recv(a) => \"got\"\n        else => \"none\"\n"
           "    print(label)\n")
    assert _run(src) == "got\n"


def test_select_loop_fan_in():
    src = ("fn produce(c: Chan[Int], n: Int) -> Unit:\n    send(c, n)\n\n"
           "fn main() -> Unit:\n    let a = chan[Int]()\n    let b = chan[Int]()\n"
           "    spawn produce(a, 1)\n    spawn produce(b, 2)\n    var got = 0\n"
           "    while got < 2:\n        select:\n"
           "            va = recv(a) =>\n                match va:\n"
           "                    Some(x) => print(x)\n                    None => print(\"a\")\n"
           "            vb = recv(b) =>\n                match vb:\n"
           "                    Some(y) => print(y)\n                    None => print(\"b\")\n"
           "        got = got + 1\n")
    assert _run(src) == "1\n2\n"


def test_timeout_idiom_data_wins():
    # data arrives before the 100ms timeout fires → data branch
    src = ("fn timeout(t: Chan[Bool]) -> Unit:\n    sleep(100)\n    send(t, true)\n\n"
           "fn feed(d: Chan[Int]) -> Unit:\n    send(d, 5)\n\n"
           "fn main() -> Unit:\n    let d = chan[Int]()\n    let t = chan[Bool]()\n"
           "    spawn timeout(t)\n    spawn feed(d)\n    select:\n"
           "        v = recv(d) => print(v)\n        done = recv(t) => print(\"timeout\")\n")
    assert _run(src) == "Some(5)\n"


def test_timeout_idiom_timeout_wins():
    # no data producer → the sleeping timeout fiber wins once the virtual clock advances
    src = ("fn timeout(t: Chan[Bool]) -> Unit:\n    sleep(100)\n    send(t, true)\n\n"
           "fn main() -> Unit:\n    let d = chan[Int]()\n    let t = chan[Bool]()\n"
           "    spawn timeout(t)\n    select:\n"
           "        v = recv(d) => print(v)\n        done = recv(t) => print(\"timeout\")\n")
    assert _run(src) == "timeout\n"


# ------------------------------------------------- W15: waiter dequeue-on-wake (leak fix)

import io

from weft.cli import compile_source
from weft.vm import VM
from weft.values import Channel
from harness import _VirtualClock


def test_server_loop_nonfiring_channel_waiters_stay_bounded():
    # The server-loop repro: 200 selects over {data, shutdown} where shutdown never fires.
    # Each iteration parks the select (the feeder sleeps first, so `data` is never ready at
    # poll time), registering a waiter on BOTH channels; when the data arm wins, the sibling
    # waiter on `shutdown` must be dequeued eagerly — not left to accumulate as a tombstone.
    src = ("let data = chan[Int]()\n"
           "let shutdown = chan[Int]()\n\n"
           "fn feeder(n: Int) -> Unit:\n"
           "    var i = 0\n"
           "    while i < n:\n"
           "        sleep(1)\n"
           "        send(data, i)\n"
           "        i = i + 1\n\n"
           "fn main() -> Unit:\n"
           "    spawn feeder(200)\n"
           "    var got = 0\n"
           "    while got < 200:\n"
           "        select:\n"
           "            v = recv(data) => got = got + 1\n"
           "            s = recv(shutdown) => got = 200\n"
           "    print(got)\n")
    code = compile_source(src, "leak.weft")
    out = io.StringIO()
    clk = _VirtualClock()
    vm = VM(stdout=out, clock=clk.now, sleep_fn=clk.sleep)
    rc = vm.run(code, src, "leak.weft")
    assert rc == 0
    assert out.getvalue() == "200\n"
    chans = [g for g in vm.globals if isinstance(g, Channel)]
    assert len(chans) == 2
    for c in chans:                        # white-box: no unbounded tombstone accumulation
        assert len(c.recv_wait) <= 1
        assert len(c.send_wait) <= 1
