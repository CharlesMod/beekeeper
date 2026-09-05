"""H-12, restart on stall (budget-law.md §6): the stall exit fires at 9–53 s
of a 480 s cap and leaves the clock unused. The proxy pins temperature 0 at
the wire for every arm, so a restart cannot vary the sampler — it varies the
CONTEXT: a fresh episode over the same tree, carrying one line that names
what the stalled attempt kept repeating and which files it changed. Laws:
  - an episode that ends stalled restarts only while the clock still holds
    a median episode and at least the floor (60 s); never otherwise;
  - the restart is a fresh context: the task text plus one note per earlier
    attempt (attempt, turns, wall, the repeated calls, the edited files);
  - a restart skips the start verify and carries the net's baseline (the
    baseline is measured before the FIRST edit; re-measuring it on an
    edited tree would hide regressions);
  - off by default, declared (`BEEKEEPER_RESTART=on`), named in the
    settings line; every restart is a record in the spend ledger.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402

A = ("bash", {"command": "echo run >> count.txt; echo same"})   # identical result every time -> stalls
B = ("bash", {"command": "echo other"})


class Scripted(Beekeeper):
    def __init__(self, arena, task, script, **kw):
        super().__init__(str(arena), task, **kw)
        self.script = list(script)

    def request(self):
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
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def _make(arena, made, scripts, **kw):
    def make(attempt, notes, prev):
        task = "the task" + ("\n\n" + "\n".join(notes) if notes else "")
        bk = Scripted(arena, task, scripts[attempt - 1], start_verify=(attempt == 1),
                      net_baseline=(prev.net_baseline if prev is not None else None), **kw)
        made.append(bk)
        return bk
    return make


def test_a_stalled_episode_restarts_with_a_note_while_the_clock_holds(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    made = []
    rc, attempts = beekeeper.run_attempts(_make(arena, made, [[A] * 9, [B]]), max_seconds=600)
    assert [a["rc"] for a in attempts][0] == 3 and len(attempts) == 2, attempts
    assert attempts[0]["reason"] == "stalled" and attempts[0]["turns"] >= 8
    note = made[1].messages[1]["content"]
    assert "Attempt 1 stalled" in note and "bash" in note and "Do not repeat" in note, note
    assert made[0].messages[1]["content"].endswith("the task"), "attempt 1 carries no note"
    assert rc == attempts[-1]["rc"]


def test_restart_needs_the_floor_and_a_median_episode():
    assert beekeeper._may_restart(left=200, walls=[120], floor=60) is True
    assert beekeeper._may_restart(left=100, walls=[120], floor=60) is False, "less than a median episode"
    assert beekeeper._may_restart(left=59, walls=[1], floor=60) is False, "less than the floor"
    assert beekeeper._may_restart(left=None, walls=[1], floor=60) is True, "no clock: no limit but the count"


def test_no_restart_without_the_floor(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    made = []
    rc, attempts = beekeeper.run_attempts(_make(arena, made, [[A] * 9, [B]]), max_seconds=30)
    assert len(attempts) == 1 and rc == 3, attempts


def test_off_by_default_never_restarts(arena, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_RESTART", raising=False)
    made = []
    rc, attempts = beekeeper.run_attempts(_make(arena, made, [[A] * 9, [B]]), max_seconds=600)
    assert len(attempts) == 1 and rc == 3


def test_a_restart_skips_the_start_verify_and_carries_the_net_baseline(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    marker = arena / "verifies.txt"
    verify = f"echo v >> {marker}; exit 1"
    made = []
    def make(attempt, notes, prev):
        bk = Scripted(arena, "the task", [[A] * 9, [B]][attempt - 1], verify_cmd=verify,
                      start_verify=(attempt == 1), net_baseline=(prev.net_baseline if prev else None))
        if attempt == 1:
            bk.net_baseline = {"tests/test_x.py::test_a"}
        made.append(bk)
        return bk
    beekeeper.run_attempts(make, max_seconds=600)
    assert len(made) == 2
    assert marker.read_text().count("v") == 1, "the start verify ran once, for attempt 1 only"
    assert made[1].net_baseline == {"tests/test_x.py::test_a"}


def test_the_restart_is_named_in_settings_and_recorded_in_the_ledger(arena, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    made = []
    beekeeper.run_attempts(_make(arena, made, [[A] * 9, [B]]), max_seconds=600)
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "settings:" in l][0]
    assert "restart=on(env)" in line, line
    recs = [json.loads(l) for f in (tmp_path / "spend").glob("*.jsonl") for l in open(f) if l.strip()]
    rs = [r for r in recs if r.get("kind") == "restart"]
    assert len(rs) == 1 and rs[0]["attempt"] == 1 and rs[0]["reason"] == "stalled" and "left" in rs[0], rs


def test_settings_default_reads_off(arena, capsys, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_RESTART", raising=False)
    Scripted(arena, "the task", [])
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "restart=off(default)" in line, line
