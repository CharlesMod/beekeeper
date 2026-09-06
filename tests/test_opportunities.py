"""H-51 (instrument-law.md M-04): the opportunity counts are a SCHEMA, not a
grep. A lever that acts at a gate is `unmeasured` when the gate was never
reached — the net was called inert on pool v2 after firing in 0 of 28 runs,
and only a hand-count of the transcripts showed that no run had ever reached
`done` green, so the gate could not have fired. That reading must come out of
the ledger.

One documented place in the END spend record — `opportunities` — carries, for
every lever, the count of times its gate was REACHED beside the count of times
it ACTED, read from the counters the levers already keep:

  restart   stalls (the gate: an episode ended by the stall law)   / restarts
  withhold  exhaustions, refusals (the gate)                       / withheld_turns
  board     board_rows (the gate: rows to flip)                    / board_flips
  net       net_baselines, net_gate_reached (the gate)             / net_refusals
  think     turns                                                  / think_turns
  create    create_offers (the gate: a missing path was seen)      / create_taken

Zero is a measurement: a lever whose gate reads 0 was unmeasured on that run,
never inert. Every end record carries the block, whatever the flags say.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402

# one visible test id: red until `fixed` exists, green after
VERIFY = ("sh -c 'if test -f fixed; then echo \"1 passed in 0.1s\"; exit 0; else "
          "echo \"FAILED tests/t.py::a - boom\"; echo \"1 failed in 0.1s\"; exit 1; fi' -- tests/t.py::a")
A = ("bash", {"command": "echo run >> count.txt; echo same"})       # identical result: stalls
EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
FIX = ("bash", {"command": "touch fixed"})
RUN_VERIFY = ("bash", {"command": VERIFY})
DONE = ("done", {"summary": "fixed the greeting"})

GATES = {"turns", "stalls", "restarts", "exhaustions", "refusals", "withheld_turns",
         "board_rows", "board_flips", "net_baselines", "net_gate_reached", "net_refusals",
         "think_turns"}


class Scripted(Beekeeper):
    def __init__(self, arena, task, script, **kw):
        super().__init__(str(arena), task, **kw)
        self.script = list(script)

    def request(self):
        self._request_body()
        if not self.script:
            return {"message": {"content": ""}, "finish_reason": "stop"}
        name, args = self.script.pop(0)
        return {"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls"}


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    for k in ("BEEKEEPER_BOARD", "BEEKEEPER_RESTART", "BEEKEEPER_CREATE", "BEEKEEPER_THINK"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def records(tmp_path):
    return [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]


def ends(tmp_path):
    return [r for r in records(tmp_path) if r.get("kind") == "end"]


def test_a_run_through_every_gate_reports_each_levers_opportunities(arena, tmp_path, monkeypatch):
    """One run, four levers: attempt 1 stalls (the restart gate), attempt 2
    edits, greens the board's row and reaches the net at done."""
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    monkeypatch.setenv("BEEKEEPER_BOARD", "on")
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    scripts = [[A] * 9, [EDIT, FIX, RUN_VERIFY, DONE]]

    def make(attempt, notes, prev):
        task = "the task" + ("\n\n" + "\n".join(notes) if notes else "")
        return Scripted(arena, task, scripts[attempt - 1], verify_cmd=VERIFY,
                        start_verify=(attempt == 1),
                        net_baseline=(prev.net_baseline if prev is not None else None))

    rc, attempts = beekeeper.run_attempts(make, max_seconds=600)
    assert [a["rc"] for a in attempts] == [3, 0], attempts

    e = ends(tmp_path)
    assert len(e) == 2, e
    for r in e:
        assert GATES <= set(r["opportunities"]), sorted(r["opportunities"])
    o1, o2 = e[0]["opportunities"], e[1]["opportunities"]

    # attempt 1: the stall law's gate was reached; the net's never was
    assert o1["stalls"] == 1 and o1["restarts"] == 0, o1
    assert o1["exhaustions"] == 1 and o1["refusals"] == 5 and o1["withheld_turns"] >= 1, o1
    assert o1["board_rows"] == 1 and o1["board_flips"] == 0, o1
    assert o1["net_baselines"] == 0 and o1["net_gate_reached"] == 0, \
        "no edit and no done: the net could not have fired — unmeasured, not inert"

    # attempt 2: the restart happened, the board flipped, the net judged
    assert o2["restarts"] == 1 and o2["stalls"] == 0, o2
    assert o2["board_rows"] == 1 and o2["board_flips"] == 1, o2
    assert o2["net_baselines"] == 1 and o2["net_gate_reached"] == 1 and o2["net_refusals"] == 0, o2
    assert o2["think_turns"] >= 1 and o2["turns"] == 4, o2

    # the restart count agrees with run_attempts' own records
    fired = [r for r in records(tmp_path) if r.get("kind") == "restart" and r.get("restarting")]
    assert len(fired) == o2["restarts"] == 1, fired


def test_every_end_record_carries_the_block_even_with_every_lever_off(arena, tmp_path):
    """Zero opportunities is a measurement the analysis script can read; an
    absent block is not."""
    bk = Scripted(arena, "the task", [EDIT])
    bk.run()
    o = ends(tmp_path)[0]["opportunities"]
    assert GATES <= set(o), sorted(o)
    assert o["stalls"] == 0 and o["restarts"] == 0 and o["board_rows"] == 0 and o["board_flips"] == 0
    assert o["net_baselines"] == 0 and o["net_gate_reached"] == 0 and o["exhaustions"] == 0
    assert o["think_turns"] == 0 and o["turns"] == bk.turn


def test_the_create_levers_counts_join_the_same_block(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CREATE", "on")
    bk = Scripted(arena, "the task", [
        ("bash", {"command": "echo \"ModuleNotFoundError: No module named 'shed'\"; exit 1"}),
        ("bash", {"command": "echo two"})])
    bk.run()
    o = ends(tmp_path)[0]["opportunities"]
    assert {"create_offers", "create_taken"} <= set(o), sorted(o)
    assert o["create_offers"] == 0 and o["create_taken"] == 0, "nothing in the arena roots `shed`"


def test_the_net_refusal_is_an_opportunity_that_acted(arena, tmp_path, monkeypatch):
    """The net's gate reached AND the net acting are two different numbers."""
    (arena / "tests").mkdir()
    (arena / "tests" / "t.py").write_text("")
    verify = ("sh -c 'if test -f fixed; then echo \"1 passed\"; exit 0; else echo \"1 failed\"; exit 1; fi'"
              " -- tests/t.py::a")
    net_out = arena / "netcalls.txt"
    # the net command (the file, not the id) reports a sibling red from the start
    monkeypatch.setenv("BEEKEEPER_NET", "on")
    bk = Scripted(arena, "the task", [EDIT, FIX, DONE, DONE], verify_cmd=verify)
    bk.net_cmd = f"sh -c 'echo call >> {net_out}; echo \"FAILED tests/t.py::b - boom\"; exit 1'"
    rc = bk.run()
    o = ends(tmp_path)[0]["opportunities"]
    assert o["net_baselines"] == 1 and o["net_gate_reached"] == 2, o
    assert o["net_refusals"] == 1, "the first done was refused once for the pre-existing sibling"
    assert rc == 0
