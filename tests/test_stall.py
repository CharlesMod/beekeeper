"""The stall law: a repeated action that returns the identical result is
not narrated, it is REFUSED — and the refusal is not the harness's memory
being reset.

pub1 (2026-09-03) and the ctx128k retest (09-04): with the context fully
intact — zero compactions, every prior result and every "identical Nx,
stop repeating" note in view — the model still re-issued the same bash
probe in a period-3 cycle, because the guard fired at 3, injected a
pivot note, and RESET its own counter (the 4th identical call counted as
fresh). Greedy decoding over a near-static context is a fixed point; a
note does not move it, an affordance does. Laws:
  - three identical (action, result) pairs exhaust that action: the 4th
    identical call is refused BEFORE it runs, and the counter never resets;
  - exhaustion survives an unrelated action in between;
  - a successful edit or write clears it (the world changed; a re-probe is
    new information);
  - five consecutive refusals end the run: exit 3, "stalled" — the turns
    saved are the throughput, and the failure class is named;
  - an action whose result differs is never refused.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

A = ("bash", {"command": "echo run >> count.txt; echo same"})        # side effect, identical output
B = ("bash", {"command": "echo other"})
GROW = ("bash", {"command": "echo run >> count.txt; cat count.txt"})  # output differs every time
EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
WRITE = ("write", {"file_path": "g.py", "content": "x = 1\n"})


class Scripted(Beekeeper):
    def __init__(self, arena, script):
        super().__init__(str(arena), "the task", verify_cmd=None)
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


def runs(arena):
    p = arena / "count.txt"
    return len(p.read_text().splitlines()) if p.exists() else 0


def test_the_fourth_identical_call_is_refused_before_it_runs(arena, capsys):
    rc = Scripted(arena, [A] * 5).run()
    out = capsys.readouterr().out
    assert runs(arena) == 3, f"the command ran {runs(arena)}x — the 4th and 5th must be refused, not executed"
    assert "refused" in out, out
    assert rc != 0


def test_exhaustion_survives_an_unrelated_action(arena, capsys):
    rc = Scripted(arena, [A, A, A, B, A]).run()
    out = capsys.readouterr().out
    assert runs(arena) == 3, "one different action in between does not un-exhaust the probe"
    assert "stalled" not in out and rc == 1, "a single refusal is not a stall"


def test_an_edit_clears_exhaustion(arena):
    Scripted(arena, [A, A, A, EDIT, A]).run()
    assert (arena / "f.py").read_text() == "greeting = 'hello world'\n"
    assert runs(arena) == 4, "after the world changed, the same probe is new information"


def test_a_write_clears_exhaustion(arena):
    Scripted(arena, [A, A, A, WRITE, A]).run()
    assert (arena / "g.py").exists()
    assert runs(arena) == 4


def test_five_consecutive_refusals_end_the_run_as_stalled(arena, capsys):
    rc = Scripted(arena, [A] * 12).run()
    out = capsys.readouterr().out
    assert rc == 3, f"exit 3 names the stall; got {rc}"
    assert "stalled" in out, out
    assert runs(arena) == 3


def test_a_changing_result_is_never_refused(arena, capsys):
    Scripted(arena, [GROW] * 5).run()
    out = capsys.readouterr().out
    assert runs(arena) == 5
    assert "refused" not in out


def test_successful_edits_to_the_same_file_are_never_exhausted(arena, capsys):
    """Progress is exempt: five distinct successful edits to one file share a
    path and a confirmation text, and none of them is a repeat."""
    edits = [("edit", {"file_path": "f.py", "old_str": f"v{i}", "new_str": f"v{i+1}"}) for i in range(5)]
    (arena / "f.py").write_text("greeting = 'v0'\n")
    Scripted(arena, edits).run()
    out = capsys.readouterr().out
    assert (arena / "f.py").read_text() == "greeting = 'v5'\n"
    assert "refused" not in out and "identical to your previous" not in out, out


def test_failing_edits_are_conflated_only_when_their_content_is_identical(arena, capsys):
    """Four failing edits with DIFFERENT old_str are a search, not a loop;
    four failing edits with the SAME old_str are a loop — the 4th is refused."""
    different = [("edit", {"file_path": "f.py", "old_str": f"missing{i}", "new_str": "x"}) for i in range(4)]
    Scripted(arena, different).run()
    out = capsys.readouterr().out
    assert "refused" not in out, out
    same = [("edit", {"file_path": "f.py", "old_str": "missing", "new_str": "x"})] * 4
    Scripted(arena, same).run()
    out = capsys.readouterr().out
    assert out.count("old_str not found") == 3 and "refused" in out, out
