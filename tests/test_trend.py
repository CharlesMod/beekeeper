"""H-02, the failing-count trend: the board carries the DIRECTION of the
verify, not only its latest count. The checkpoint law already scores every
verify (`failing_count`); until now that scalar died in the spend ledger and
the model saw a fresh red wall each time. Laws (BEEKEEPER_TREND=on; off by
default, the transcript byte-identical when off):

  - off by default; `BEEKEEPER_TREND=on` declares it and the settings line
    names it with its source, exactly as board/phase/alt do;
  - after each verify OBSERVATION the board's head line updates:
    `red 2/2 → red 1/2 improving`; the denominator is the visible set;
  - the first observation of a run has no predecessor: it renders the count
    alone, with no arrow and no direction word;
  - a flat trend is rendered flat, an improvement improving, a regression
    regressing — the word is never inferred by the model from two numbers;
  - M-02: a verify that measured nothing (exit 4/5, "no tests ran", an
    opaque red with no summary) does NOT move the trend;
  - the spend ledger's turn record carries the same trend, structured;
  - with the flag off the transcript is what H-01 alone renders, byte for
    byte, and the ledger carries no trend key.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

IDS = "tests/t.py::a tests/t.py::b"
# the start verify: both visible tests red
RED2 = ("sh -c 'echo \"FAILED tests/t.py::a - boom\"; echo \"FAILED tests/t.py::b - boom\"; "
        "echo \"2 failed in 0.1s\"; exit 1' -- " + IDS)
# the model re-runs the verify itself: each command carries `pytest` so the
# harness reads it as a verify, and each output differs so nothing collapses
V_ONE = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - boom\"; "
                             "echo \"1 failed, 1 passed in 0.2s\"; exit 1' -- pytest " + IDS})
V_ONE_AGAIN = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - still\"; "
                                   "echo \"1 failed, 1 passed in 0.3s\"; exit 1' -- pytest " + IDS})
V_TWO = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - boom\"; "
                             "echo \"FAILED tests/t.py::b - back\"; "
                             "echo \"2 failed in 0.4s\"; exit 1' -- pytest " + IDS})
V_GREEN = ("bash", {"command": "sh -c 'echo \"2 passed in 0.5s\"; exit 0' -- pytest " + IDS})
# M-02: exit 4, no signature phrase — the run measured nothing
V_UNMEASURED = ("bash", {"command": "sh -c 'echo \"usage: pytest: error: unrecognized arguments\"; "
                                    "exit 4' -- pytest " + IDS})

TREND_SEG = re.compile(r" · trend [^\]\n]*")


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify_cmd=RED2, **kw):
        super().__init__(str(arena), "the task", verify_cmd=verify_cmd, **kw)
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
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    monkeypatch.setenv("BEEKEEPER_BOARD", "on")
    monkeypatch.setenv("BEEKEEPER_TREND", "on")
    for k in ("BEEKEEPER_LADDER", "BEEKEEPER_RESTART", "BEEKEEPER_CREATE", "BEEKEEPER_THINK",
              "BEEKEEPER_ALT", "BEEKEEPER_PHASE", "BEEKEEPER_AUTOVERIFY"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def board(bk):
    b = [m for m in bk.messages
         if m.get("role") == "user" and str(m.get("content", "")).startswith("[Board")]
    assert len(b) <= 1, b
    return b[0]["content"] if b else ""


def head(bk):
    return board(bk).splitlines()[0] if board(bk) else ""


def records(tmp_path):
    return [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]


def settings(capsys):
    return [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]


# --- the flag ---

def test_off_by_default_and_named_in_the_settings_line(arena, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BEEKEEPER_TREND")
    bk = Scripted(arena, [V_ONE])
    bk.run()
    assert bk.trend_policy == "off"
    assert "trend=off(default)" in settings(capsys)
    assert TREND_SEG.search(board(bk)) is None, board(bk)
    assert bk.trend_history == []
    assert not [r for r in records(tmp_path) if r.get("trend") is not None], "no trend key when off"


def test_on_is_declared_in_the_settings_line(arena, capsys):
    Scripted(arena, [])
    assert "trend=on(env)" in settings(capsys)


# --- the direction ---

def test_the_first_observation_renders_the_count_with_no_direction(arena):
    """A run's first verify has no predecessor: two numbers would be a lie."""
    bk = Scripted(arena, [("bash", {"command": "echo hi"})])
    bk.run()
    h = head(bk)
    assert "trend red 2/2" in h, h
    assert "→" not in h and "improving" not in h and "flat" not in h, h


def test_an_improvement_is_rendered_improving(arena):
    bk = Scripted(arena, [V_ONE])
    bk.run()
    h = head(bk)
    assert "trend red 2/2 → red 1/2 improving" in h, h


def test_a_flat_trend_is_rendered_flat(arena):
    bk = Scripted(arena, [V_ONE, V_ONE_AGAIN])
    bk.run()
    h = head(bk)
    assert "trend red 1/2 → red 1/2 flat" in h, h


def test_a_regression_is_rendered_regressing(arena):
    bk = Scripted(arena, [V_ONE, V_TWO])
    bk.run()
    h = head(bk)
    assert "trend red 1/2 → red 2/2 regressing" in h, h


def test_green_is_rendered_green_and_zero(arena):
    bk = Scripted(arena, [V_ONE, V_GREEN])
    bk.run()
    h = head(bk)
    assert "trend red 1/2 → green 0/2 improving" in h, h


def test_an_unmeasured_verify_does_not_move_the_trend(arena):
    """M-02: a red exit is not evidence that tests ran."""
    bk = Scripted(arena, [V_ONE, V_UNMEASURED])
    bk.run()
    h = head(bk)
    assert "trend red 2/2 → red 1/2 improving" in h, h
    assert [n for _, n in bk.trend_history] == [2, 1], bk.trend_history


# --- the ledger ---

def test_the_spend_ledgers_turn_record_carries_the_trend(arena, tmp_path):
    bk = Scripted(arena, [V_ONE])
    bk.run()
    turns = [r for r in records(tmp_path) if r.get("kind") == "turn" and r.get("trend")]
    assert turns, [r for r in records(tmp_path) if r.get("kind") == "turn"]
    t = turns[0]["trend"]
    assert t["prev"] == 2 and t["now"] == 1 and t["total"] == 2 and t["dir"] == "improving", t


def test_the_end_record_carries_the_trends_opportunity_counts(arena, tmp_path):
    """M-04: effects beside opportunities, in every arm."""
    bk = Scripted(arena, [V_ONE, V_TWO])
    bk.run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert {"trend_observations", "trend_improvements", "trend_regressions"} <= set(o), sorted(o)
    assert o["trend_observations"] == 3 and o["trend_improvements"] == 1 and o["trend_regressions"] == 1, o


def test_the_trend_is_measured_even_with_no_board(arena, tmp_path, monkeypatch):
    """The board is where it is SHOWN; the ledger records it either way."""
    monkeypatch.delenv("BEEKEEPER_BOARD")
    bk = Scripted(arena, [V_ONE])
    bk.run()
    assert board(bk) == "", "no board without H-01"
    assert [n for _, n in bk.trend_history] == [2, 1], bk.trend_history
    assert [r for r in records(tmp_path) if r.get("kind") == "turn" and r.get("trend")]


# --- the blast radius ---

def test_off_the_transcript_is_the_board_alone_byte_for_byte(arena, tmp_path, monkeypatch):
    """The only byte the flag may move is the board's head."""
    script = [V_ONE, ("bash", {"command": "echo hi"})]
    monkeypatch.delenv("BEEKEEPER_TREND")
    off = Scripted(arena, list(script))
    off.run()
    monkeypatch.setenv("BEEKEEPER_TREND", "on")
    on = Scripted(arena, list(script))
    on.run()
    assert len(off.messages) == len(on.messages), (len(off.messages), len(on.messages))
    boards = 0
    for a, b in zip(off.messages, on.messages):
        if str(b.get("content", "")).startswith("[Board"):
            boards += 1
            assert TREND_SEG.search(b["content"]), b["content"]
            assert TREND_SEG.search(a["content"]) is None, a["content"]
            assert TREND_SEG.sub("", b["content"]) == a["content"]
        else:
            assert a == b, (a, b)
    assert boards == 1
