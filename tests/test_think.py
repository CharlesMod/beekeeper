"""Two rungs of one model: the harness chooses thinking per turn.

The ring measured Ling-3.0-tiny two ways: with the template's default on,
the naive arm narrated untagged for 4,096 tokens and never reached a diff;
with it off, a three-second diff writer. Nobody measured thinking INSIDE
the loop, on the turns where it might pay (diagnosis) and never on the
turns where it cannot (an edit, a verify). Laws:
  - BEEKEEPER_THINK unset: OFF, sent explicitly (amended 09-05: a silent
    "say nothing" let the server decide under a harness-chosen pin);
  - off / on: every turn asks explicitly for that setting;
  - phase: think on turn 1, think on the turn after a red verify, never on
    the turn after an edit or a green verify; the decision is logged per
    turn so a transcript records it — measured, not asserted;
  - a thinking turn raises max_tokens by the thinking budget so the
    tool call still fits after the think block closes.
"""
import copy
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


def test_unset_policy_is_explicit_off(arena):
    """Amended 2026-09-05 (silent-defaults.md): saying nothing let the server's
    default decide under a harness-chosen pin. Unset is OFF, said out loud."""
    bk = Scripted(arena, [EDIT])
    bk.run()
    assert all(_thinking(b) is False for b in bk.bodies), bk.bodies[0].get("chat_template_kwargs")


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


# --- the smarter budget: the server's is the ceiling, the harness spends it per turn ---

OVERFLOW = {"message": {"content": "", "reasoning_content": "the bug is probably in the max removal step and "},
            "finish_reason": "length"}


class Budgeted(Scripted):
    """A scripted model that can answer a thinking turn with an overflow
    (cut mid-thought by max_tokens) before the scripted tool call."""
    def __init__(self, arena, script, overflow_on=()):
        super().__init__(arena, script)
        self.overflow_on = set(overflow_on)
        self.calls = 0

    def request(self):
        self.calls += 1
        body = self._request_body()
        self.bodies.append(copy.deepcopy(body))   # the live body shares self.messages
        if self.turn in self.overflow_on and (body.get("chat_template_kwargs") or {}).get("enable_thinking"):
            self.overflow_on.discard(self.turn)
            return OVERFLOW
        return super().request() if False else self._scripted()

    def _scripted(self):
        if not self.script:
            return {"message": {"content": ""}, "finish_reason": "stop"}
        name, args = self.script.pop(0)
        return {"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls"}


def _budget(body):
    return body["max_tokens"] - Beekeeper.ANSWER_ROOM


def test_budget_scales_with_the_phase(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    monkeypatch.setenv("BEEKEEPER_THINK_BUDGET", "512")
    # t1 (turn one: 1x) edit · t2 verify red · t3 (after red: 2x) edit · t4 verify red
    # · t5 (two failed edits since a green: 4x)
    bk = Budgeted(arena, [EDIT, RUN_VERIFY, EDIT, RUN_VERIFY, FIX])
    bk.run()
    assert _budget(bk.bodies[0]) == 512
    assert _budget(bk.bodies[2]) == 1024
    assert _budget(bk.bodies[4]) == 2048


def test_budget_never_exceeds_the_ceiling_or_a_quarter_of_the_clock(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "on")
    monkeypatch.setenv("BEEKEEPER_THINK_BUDGET", "3000")
    monkeypatch.setenv("BEEKEEPER_THINK_CEILING", "2048")
    bk = Budgeted(arena, [EDIT])
    bk.run()
    assert _budget(bk.bodies[0]) == 2048
    bk = Budgeted(arena, [EDIT])
    bk.tok_rate = 100.0                       # tokens/s, as measured
    bk.run(max_seconds=40)                    # a quarter of 40 s at 100 tok/s = 1000
    assert _budget(bk.bodies[0]) <= 1000


def test_an_overflow_continues_the_turn_with_thinking_off_and_doubles_the_next(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "on")
    monkeypatch.setenv("BEEKEEPER_THINK_BUDGET", "512")
    bk = Budgeted(arena, [EDIT, RUN_VERIFY, FIX], overflow_on={1})
    bk.run()
    first, cont = bk.bodies[0], bk.bodies[1]
    assert (cont.get("chat_template_kwargs") or {}).get("enable_thinking") is False, "the continuation acts, it does not think again"
    assert "max removal step" in cont["messages"][-1]["content"], "the partial thought rides the continuation"
    assert cont["messages"][-1]["role"] == "user"
    assert _budget(bk.bodies[2]) == 1024, "an overflow doubles the next thinking budget"
    assert (arena / "f.py").read_text() == "greeting = 'hello world'\n", "the continuation's tool call ran"


def test_a_turn_that_closes_early_halves_the_next_budget_toward_the_base(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_THINK", "on")
    monkeypatch.setenv("BEEKEEPER_THINK_BUDGET", "512")
    # no verify in between (a red one would scale by phase), one edit only (two
    # without a green would scale 4x): the adaptive term alone is under test
    bk = Budgeted(arena, [EDIT, FIX, RUN_VERIFY], overflow_on={1})
    bk.run()
    # t1 overflowed -> t2 budget 1024; t2 closed itself using ~0 -> t3 back to 512
    assert _budget(bk.bodies[2]) == 1024 and _budget(bk.bodies[3]) == 512


def test_spend_is_logged(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_THINK", "on")
    Budgeted(arena, [EDIT], overflow_on={1}).run()
    out = capsys.readouterr().out
    assert "budget=" in out and "closed=overflow" in out, out
