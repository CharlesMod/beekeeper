"""The spend ledger (budget-law.md §7): one JSONL record per turn, one at the
end — every budget decision and every spend, auditable without reading a
transcript. Laws:
  - BEEKEEPER_SPEND_DIR set: one record per turn with the turn, elapsed
    seconds, phase, the thinking decision and spend, the action, the verify
    verdict and the parsed failing count, context size, compactions; plus a
    final record with the totals and the reason the run ended;
  - unset: nothing is written anywhere;
  - the records agree with the transcript's think log.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

VERIFY = "sh -c 'if test -f fixed; then echo \"3 passed\"; else echo \"2 failed, 1 passed\"; fi; test -f fixed'"
EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
RUN_VERIFY = ("bash", {"command": VERIFY})
FIX = ("bash", {"command": "touch fixed"})


class Scripted(Beekeeper):
    def __init__(self, arena, script):
        super().__init__(str(arena), "the task", verify_cmd=VERIFY)
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
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def _records(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


def test_one_record_per_turn_and_a_final_one(arena, tmp_path):
    bk = Scripted(arena, [EDIT, RUN_VERIFY, FIX, RUN_VERIFY])
    bk.run()
    recs = _records(tmp_path)
    turns = [r for r in recs if r.get("kind") == "turn"]
    final = [r for r in recs if r.get("kind") == "end"]
    assert len(turns) == bk.turn and len(final) == 1, (len(turns), bk.turn, final)
    for k in ("turn", "t", "phase", "think", "budget", "used", "closed", "action", "sig",
              "refused", "verify", "failing", "ctx_chars", "compactions", "max_tokens"):
        assert k in turns[0], k
    assert final[0]["reason"] and final[0]["turns"] == bk.turn
    assert final[0]["arena"] == str(arena)


def test_verify_verdict_and_failing_count_are_parsed(arena, tmp_path):
    Scripted(arena, [EDIT, RUN_VERIFY, FIX, RUN_VERIFY]).run()
    turns = [r for r in _records(tmp_path) if r.get("kind") == "turn"]
    red, green = turns[1], turns[3]
    assert red["action"] == "bash" and red["verify"] == "red" and red["failing"] == 2
    assert green["verify"] == "green" and green["failing"] == 0
    assert turns[0]["verify"] is None


def test_records_agree_with_the_think_log(arena, tmp_path):
    bk = Scripted(arena, [EDIT, RUN_VERIFY, FIX])
    bk.run()
    turns = [r for r in _records(tmp_path) if r.get("kind") == "turn"]
    assert [(r["turn"], r["think"]) for r in turns[:3]] == bk.think_log[:3]
    assert turns[0]["phase"] == "turn_one" and turns[2]["phase"] == "after_red_verify"


def test_nothing_is_written_without_the_dir(arena, tmp_path, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_SPEND_DIR")
    Scripted(arena, [EDIT]).run()
    assert not (tmp_path / "spend").exists()
    assert not list(tmp_path.glob("**/*.jsonl"))


def test_ledger_names_are_unique_per_arena_and_records_carry_the_arena(tmp_path, monkeypatch):
    """Inside a container the worker is PID 1 and four workers start in the
    same second: a name of time+pid collided (pool v2: 25 ledgers for 28 runs,
    turn records unattributable). The name carries the arena's hash and every
    record carries the arena."""
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    a1, a2 = tmp_path / "a1", tmp_path / "a2"
    a1.mkdir(); a2.mkdir()
    b1, b2 = Scripted(a1, [EDIT]), Scripted(a2, [EDIT])
    assert b1.spend_path != b2.spend_path
    (a1 / "f.py").write_text("greeting = 'hello'\n"); (a2 / "f.py").write_text("greeting = 'hello'\n")
    b1.run(); b2.run()
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 2
    for f in files:
        recs = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        assert all("arena" in r for r in recs), recs[0]
        assert len({r["arena"] for r in recs}) == 1
