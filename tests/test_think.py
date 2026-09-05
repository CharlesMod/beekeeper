"""Two rungs of one model: the harness chooses thinking per turn.

The ring measured Ling-3.0-tiny two ways: with the template's default on,
the naive arm narrated untagged for 4,096 tokens and never reached a diff;
with it off, a three-second diff writer. Nobody measured thinking INSIDE
the loop, on the turns where it might pay (diagnosis) and never on the
turns where it cannot (an edit, a verify). Laws:
  - BEEKEEPER_THINK unset: the request body carries no thinking field at
    all (byte-identical to the worker before this law, so a pinned proxy
    that forces the setting sees nothing new);
  - off / on: every turn asks explicitly for that setting;
  - phase: think on turn 1, think on the turn after a red verify, never on
    the turn after an edit or a green verify; the decision is logged per
    turn so a transcript records it — measured, not asserted;
  - a thinking turn raises max_tokens by the thinking budget so the
    tool call still fits after the think block closes.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper, THINK_BUDGET  # noqa: E402

VERIFY = "sh -c 'test -f fixed'"
EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
RUN_VERIFY = ("bash", {"command": VERIFY})
FIX = ("bash", {"command": "touch fixed"})


class Scripted(Beekeeper):
    def __init__(self, arena, script):
        super().__init__(str(arena), "the task", verify_cmd=VERIFY)
        self.script = list(script)
        self.bodies = []

    def request(self):
        self.bodies.append(self._request_body())
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
    monkeypatch.delenv("BEEKEEPER_THINK", raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def _thinking(body):
    return (body.get("chat_template_kwargs") or {}).get("enable_thinking", "absent")


def test_unset_policy_sends_no_thinking_field(arena):
    bk = Scripted(arena, [EDIT])
    bk.run()
    assert all(_thinking(b) == "absent" for b in bk.bodies), bk.bodies[0].keys()
    assert "chat_template_kwargs" not in bk.bodies[0]


def test_off_and_on_ask_explicitly_every_turn(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "off")
    bk = Scripted(arena, [EDIT, RUN_VERIFY])
    bk.run()
    assert {_thinking(b) for b in bk.bodies} == {False}
    monkeypatch.setenv("BEEKEEPER_THINK", "on")
    bk = Scripted(arena, [EDIT, RUN_VERIFY])
    bk.run()
    assert {_thinking(b) for b in bk.bodies} == {True}


def test_phase_policy_thinks_on_turn_one_and_after_a_red_verify_only(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    # t1 edit (think: first turn) · t2 verify RED · t3 (think: after red) fix
    # · t4 verify GREEN · t5 (no think: after green) edit
    bk = Scripted(arena, [EDIT, RUN_VERIFY, FIX, RUN_VERIFY, EDIT])
    bk.run()
    decisions = [(t, think) for t, think in bk.think_log]
    assert decisions[:5] == [(1, True), (2, False), (3, True), (4, False), (5, False)], decisions
    assert [_thinking(b) for b in bk.bodies[:5]] == [True, False, True, False, False]


def test_a_thinking_turn_carries_the_budget_in_max_tokens(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    bk = Scripted(arena, [EDIT, RUN_VERIFY, FIX])
    bk.run()
    think_turn, plain_turn = bk.bodies[0], bk.bodies[1]
    assert think_turn["max_tokens"] >= plain_turn["max_tokens"] + THINK_BUDGET


def test_the_decision_is_written_to_the_transcript(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    Scripted(arena, [EDIT]).run()
    out = capsys.readouterr().out
    assert "think=on" in out and "think=off" in out, out
