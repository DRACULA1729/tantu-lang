"""Concurrency tests (DESIGN.md §8.1): determinism across budgets, channels, deadlock, panics.

Concurrent programs are run through the in-process harness (virtual clock), so scheduling is
deterministic; running the same program at the default budget and at budget=1 (maximal
interleaving) must produce identical output.
"""

from __future__ import annotations

import pytest

from harness import run_capture

SHOWCASE = (
    "enum Shape:\n    Circle(Float)\n    Rect(Float, Float)\n\n"
    "fn area(s: Shape) -> Float:\n    match s:\n        Circle(r) => 3.14159 * r * r\n"
    "        Rect(w, h) => w * h\n\n"
    "fn worker(jobs: Chan[Shape], out: Chan[Float]) -> Unit:\n"
    "    loop:\n        match recv(jobs):\n            Some(s) => send(out, area(s))\n            None => break\n\n"
    "fn main() -> Unit:\n    let jobs = chan[Shape](8)\n    let out = chan[Float](8)\n"
    "    spawn worker(jobs, out)\n    send(jobs, Circle(1.0))\n    send(jobs, Rect(3.0, 4.0))\n    close(jobs)\n"
    "    print(recv(out))\n    print(recv(out))\n"
)

PINGPONG = (
    "fn pong(c: Chan[Int], d: Chan[Int]) -> Unit:\n"
    "    loop:\n        match recv(c):\n            Some(n) => send(d, n + 1)\n            None => break\n\n"
    "fn main() -> Unit:\n    let c = chan[Int]()\n    let d = chan[Int]()\n    spawn pong(c, d)\n"
    "    var i = 0\n    while i < 4:\n        send(c, i)\n        match recv(d):\n            Some(r) => print(r)\n            None => break\n        i = i + 1\n    close(c)\n"
)


@pytest.mark.parametrize("budget", [1, 3, 1000])
def test_showcase_is_deterministic_across_budgets(budget):
    out, err, code = run_capture(SHOWCASE, "showcase.weft", budget=budget)
    assert code == 0 and err == ""
    assert out == "Some(3.14159)\nSome(12.0)\n"


@pytest.mark.parametrize("budget", [1, 2, 1000])
def test_pingpong_deterministic(budget):
    out, _, code = run_capture(PINGPONG, "pp.weft", budget=budget)
    assert code == 0
    assert out == "1\n2\n3\n4\n"


def test_deadlock_detected():
    out, _, code = run_capture("fn main() -> Unit:\n    let c = chan[Int]()\n    print(recv(c))\n",
                               "dl.weft")
    assert code == 1 and "R005" in out


def test_send_on_closed_panics():
    out, _, code = run_capture(
        "fn main() -> Unit:\n    let c = chan[Int](1)\n    close(c)\n    send(c, 1)\n", "sc.weft")
    assert code == 1 and "R003" in out


def test_close_of_closed_panics():
    out, _, code = run_capture(
        "fn main() -> Unit:\n    let c = chan[Int](1)\n    close(c)\n    close(c)\n", "cc.weft")
    assert code == 1 and "R004" in out


def test_sleep_orders_by_wake_time():
    src = ("fn delayed(c: Chan[Int], ms: Int, v: Int) -> Unit:\n    sleep(ms)\n    send(c, v)\n\n"
           "fn main() -> Unit:\n    let c = chan[Int](8)\n"
           "    spawn delayed(c, 30, 3)\n    spawn delayed(c, 10, 1)\n    spawn delayed(c, 20, 2)\n"
           "    print(recv(c))\n    print(recv(c))\n    print(recv(c))\n")
    out, _, code = run_capture(src, "sl.weft")
    assert code == 0 and out == "Some(1)\nSome(2)\nSome(3)\n"   # recv returns Option, ordered by wake


def test_fanin_total_independent_of_interleaving():
    src = ("fn prod(out: Chan[Int], done: Chan[Int], b: Int) -> Unit:\n"
           "    var i = 0\n    while i < 3:\n        send(out, b + i)\n        i = i + 1\n    send(done, 1)\n\n"
           "fn main() -> Unit:\n    let out = chan[Int](32)\n    let done = chan[Int](8)\n"
           "    spawn prod(out, done, 10)\n    spawn prod(out, done, 20)\n    spawn prod(out, done, 30)\n"
           "    var d = 0\n    while d < 3:\n        match recv(done):\n            Some(_) => d = d + 1\n            None => break\n"
           "    close(out)\n    var total = 0\n    loop:\n        match recv(out):\n            Some(v) => total = total + v\n            None => break\n    print(total)\n")
    for budget in (1, 1000):
        out, _, code = run_capture(src, "fi.weft", budget=budget)
        assert code == 0 and out == "189\n"      # 10+11+12 + 20+21+22 + 30+31+32
