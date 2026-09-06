"""H-32, the auto-verify: the harness runs the verify after every edit and
the result is the next observation — progress becomes visible without the
model asking for it.

Pool v2's dominant class is commitment: 92 of 125 unsolved arm-runs never
edited a file, and the runs that did edit often never looked at what the
edit did. An instruction to "re-run the tests" is text the model may obey;
an observation it did not ask for is a fact in its context. So the harness
runs the check itself, on its own clock, and reports exit code, failing
count and the tail — the same `_run_verify` the start gate and `done` use.

Laws:
  - off by default; `BEEKEEPER_AUTOVERIFY=on` declares it, the settings
    line names it, and with it off the run is byte-identical;
  - after every successful `edit` or `write` the harness verifies, and the
    result is appended as the next observation (a tool message the harness
    authored, named as the harness's, never attributed to the model);
  - budgeted: at most once a turn, never twice without an edit between,
    skipped when the last verify cost more than BEEKEEPER_AUTOVERIFY_MAX_S
    (default 90 s — a docker verify costs 20-90 s) or when less than that
    many seconds remain on the clock; a skip is a one-line note (once per
    reason, so the note is never itself an attractor) and is counted;
  - every downstream law sees it exactly as a model-run verify: the board
    (`_observe`), `last_verify_red`, `edits_since_green`, the phase;
  - the stall law never sees it: the harness's own observation is not the
    model's action — no signature, no exhaustion, no withheld tool;
  - M-04: the ledger carries one `autoverify` record per run and the end
    record carries `autoverifies` and `autoverify_skipped` — the
    opportunity counts, so a lever with no opportunity is never called
    inert.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

# names two visible ids (the board parses them) and flips on the file `fixed`
VERIFY = ("sh -c 'if test -f fixed; then echo \"2 passed\"; else "
          "echo \"FAILED tests/t.py::a - boom\"; echo \"1 failed, 1 passed\"; fi; test -f fixed' "
          "-- tests/t.py::a tests/t.py::b")
SLOW = "sh -c 'sleep 0.4; echo \"1 failed, 1 passed\"; test -f fixed' -- tests/t.py::a"

EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
EDIT2 = ("edit", {"file_path": "f.py", "old_str": "greeting", "new_str": "salute"})
FIX = ("write", {"file_path": "fixed", "content": "y\n"})
BASH = ("bash", {"command": "echo hi"})
PROBE = ("bash", {"command": "echo run >> count.txt; echo same"})   # identical output, side effect


class Scripted(Beekeeper):
    """One script entry per turn; a list entry is a multi-call turn."""

    def __init__(self, arena, script, verify=VERIFY):
        super().__init__(str(arena), "the task", verify_cmd=verify)
        self.script = list(script)

    def request(self):
        self._request_body()
        if not self.script:
            return {"message": {"content": ""}, "finish_reason": "stop"}
        item = self.script.pop(0)
        calls = item if isinstance(item, list) else [item]
        return {"message": {"content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]}, "finish_reason": "tool_calls"}


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_AUTOVERIFY", "on")
    monkeypatch.setenv("BEEKEEPER_NET", "off")          # one lever at a time
    for k in ("BEEKEEPER_AUTOVERIFY_MAX_S", "BEEKEEPER_BOARD", "BEEKEEPER_THINK",
              "BEEKEEPER_SPEND_DIR", "BEEKEEPER_RESTART"):
        monkeypatch.delenv(k, raising=False)
    return _arena(tmp_path, "arena")


def _arena(tmp_path, name):
    a = tmp_path / name
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def _av(bk):
    return [m for m in bk.messages if "auto-verify" in str(m.get("content") or "")]


def _records(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


def test_off_by_default_and_declared_in_the_settings_line(arena, tmp_path, monkeypatch, capsys):
    """Off, the run is what it was: no observation, no counts, no cost."""
    monkeypatch.delenv("BEEKEEPER_AUTOVERIFY")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [EDIT, BASH])
    bk.run()
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "autoverify=off(default)" in line, line
    assert not _av(bk)
    assert bk.autoverifies == 0 and bk.autoverify_skipped == 0
    recs = _records(tmp_path)
    assert not [r for r in recs if r.get("kind") == "autoverify"]
    end = [r for r in recs if r.get("kind") == "end"][0]
    assert end["autoverifies"] == 0 and end["autoverify_skipped"] == 0, "M-04: zero is reported, not hidden"


def test_an_edit_is_verified_by_the_harness_and_the_result_is_the_next_observation(arena, capsys):
    bk = Scripted(arena, [EDIT, BASH])
    bk.run()
    assert "autoverify=on(env)" in [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    i = next(i for i, m in enumerate(bk.messages)
             if m.get("role") == "tool" and "replaced 1 occurrence" in str(m.get("content")))
    obs = bk.messages[i + 1]
    assert obs.get("role") == "tool", obs
    body = obs["content"]
    assert "auto-verify" in body and "harness" in body, body       # never attributed to the model
    assert "exit 1" in body and "1 failing" in body, body
    assert "FAILED tests/t.py::a" in body, body                    # the tail
    assert bk.autoverifies == 1 and bk.autoverify_skipped == 0


def test_a_write_is_verified_too_and_a_green_verify_is_reported_green(arena):
    bk = Scripted(arena, [FIX])
    bk.run()
    assert bk.autoverifies == 1
    body = _av(bk)[-1]["content"]
    assert "exit 0" in body and "0 failing" in body, body
    assert bk.last_verify_red is False


def test_at_most_once_a_turn_and_never_twice_without_an_edit_between(arena, tmp_path):
    """Two edits in one turn buy one verify; two bare probes buy none; the
    next edit buys the next one."""
    bk = Scripted(arena, [[EDIT, EDIT2], BASH, BASH])
    bk.run()
    assert bk.autoverifies == 1, "one turn, one verify"
    assert (arena / "f.py").read_text() == "salute = 'hello world'\n", "both edits applied"

    b2 = _arena(tmp_path, "arena2")
    bk2 = Scripted(b2, [EDIT, BASH, EDIT2])
    bk2.run()
    assert bk2.autoverifies == 2, "an edit between is what buys the second"


def test_a_verify_over_the_budget_is_skipped_with_one_note_and_counted(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_AUTOVERIFY_MAX_S", "0.2")
    bk = Scripted(arena, [EDIT, EDIT2], verify=SLOW)
    bk.run()
    assert bk.autoverifies == 0 and bk.autoverify_skipped == 2, "the gate was reached twice and refused twice"
    notes = [m for m in bk.messages if "auto-verify skipped" in str(m.get("content") or "")]
    assert len(notes) == 1, f"one line, once per reason — a repeated note is an attractor: {notes}"
    assert "0.2" in notes[0]["content"], notes[0]["content"]
    assert "auto-verify skipped" in capsys.readouterr().out


def test_a_clock_that_cannot_afford_it_skips(arena, tmp_path):
    bk = Scripted(arena, [EDIT])
    bk.run(max_seconds=5)                    # less than the 90 s budget remains
    assert bk.autoverifies == 0 and bk.autoverify_skipped == 1
    assert [m for m in bk.messages if "auto-verify skipped" in str(m.get("content") or "")]

    bk2 = Scripted(_arena(tmp_path, "arena3"), [EDIT])
    bk2.run(max_seconds=3600)
    assert bk2.autoverifies == 1 and bk2.autoverify_skipped == 0


def test_the_board_the_phase_and_the_counters_see_it_as_a_verify(arena, tmp_path, monkeypatch):
    """Every downstream law reads it exactly as a model-run verify."""
    monkeypatch.setenv("BEEKEEPER_BOARD", "on")
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [EDIT, BASH, FIX, BASH])
    bk.run()
    turns = [r for r in _records(tmp_path) if r.get("kind") == "turn"]
    assert turns[0]["verify"] == "red" and turns[0]["failing"] == 1, turns[0]
    assert turns[1]["phase"] == "after_red_verify", turns[1]
    assert turns[2]["verify"] == "green" and turns[2]["failing"] == 0, turns[2]
    assert turns[3]["phase"] == "after_green_verify", turns[3]
    assert bk.board_rows["tests/t.py::a"][0] == "green", bk.board_rows
    assert (3, "tests/t.py::a") in bk.board_flips, bk.board_flips   # flipped by an auto-verify:
    assert not [m for m in bk.messages if m.get("role") == "assistant"     # the model never ran one
                and any(tc["function"]["name"] == "bash" and "pytest" in str(tc)
                        for tc in (m.get("tool_calls") or []))]
    assert bk.edits_since_green == 0, "a green auto-verify closes the edit streak"


def test_the_ledger_carries_every_run_and_the_end_record_the_counts(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [EDIT, FIX])
    bk.run()
    recs = _records(tmp_path)
    av = [r for r in recs if r.get("kind") == "autoverify"]
    assert len(av) == 2, av
    assert av[0]["turn"] == 1 and av[0]["code"] == 1 and av[0]["failing"] == 1
    assert av[1]["turn"] == 2 and av[1]["code"] == 0 and av[1]["failing"] == 0
    assert all(isinstance(r["wall"], (int, float)) and r["wall"] >= 0 for r in av), av
    assert all(r["arena"] == str(arena) for r in av), av
    end = [r for r in recs if r.get("kind") == "end"][0]
    assert end["autoverifies"] == 2 and end["autoverify_skipped"] == 0


def test_the_stall_law_never_sees_the_harness_own_verify(arena):
    """The auto-verify is an observation, not an action: it clears nothing,
    exhausts nothing, withholds nothing — and the model's own repeats still
    collapse to a single call in the context (the pop that collapses a
    repeat must still take the assistant message, not the auto-verify)."""
    bk = Scripted(arena, [EDIT] + [PROBE] * 5)
    bk.run()
    assert bk.autoverifies == 1
    assert (arena / "count.txt").read_text().count("\n") == 3, "the 4th and 5th probes are refused"
    assert bk.exhausted, "the probe is exhausted exactly as it would be without the lever"
    probes = [m for m in bk.messages if m.get("role") == "assistant"
              and any(tc["function"]["name"] == "bash" for tc in (m.get("tool_calls") or []))]
    assert len(probes) == 1, f"the repeat is collapsed out of the context: {probes}"
