"""H-10, the phase law: the schema itself carries the phase, so orientation
and commitment are AFFORDANCES rather than instructions.

The ring's dominant failure class is commitment latency: 92 of 125 unsolved
arm-runs never edited a file, in every harness, while the winners edit by
turn 6. The review's verdict (harness-review.md, M-09) is that a rule the
model must obey is text it will not follow; a tool that is not in the
schema is a fact it cannot argue with. Laws:
  - off by default; `BEEKEEPER_PHASE=on` declares it, `BEEKEEPER_PHASE_K`
    sets k (default 6), and the settings line names both with their source;
  - turn one carries no `edit`/`write`: the model reads or runs the verify
    first. `edit` and `write` enter the schema once a verify has been
    OBSERVED — the start verify counts;
  - after a red verify that FOLLOWS an edit, `edit` leaves the schema until
    a `read` or `bash` executes: anchoring on one region after red is the
    failure, and re-observing is the cure;
  - if turn k arrives with no edit or write yet, `read` and `bash` leave the
    schema, so the only actions left are `edit`, `write` and `done`, and one
    line says so — the first edit, forced. A refused or failed edit is not
    an edit;
  - every transition is a spend-ledger record {"kind":"phase", turn, schema,
    reason}, and the end record carries the opportunity counts M-04 needs:
    first_edit_turn, forced_edit, edit_withheld_after_red (with red_after_edit
    and forced_turns as the opportunities themselves, counted in EVERY arm —
    a lever whose gate was never reached is unmeasured, not inert).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

START_RED = ("sh -c 'echo \"FAILED tests/t.py::a - boom\"; "
             "echo \"1 failed, 1 passed in 0.1s\"; exit 1'")
# the model re-running the tests itself: 'pytest' in the command is what the
# worker reads as a verify
RED_RUN = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - boom\"; exit 1' "
                               "-- python -m pytest -q tests/t.py"})
GREEN_RUN = ("bash", {"command": "sh -c 'echo \"2 passed in 0.1s\"; exit 0' "
                                 "-- python -m pytest -q tests/t.py"})
EDIT = ("edit", {"file_path": "f.py", "old_str": "hello", "new_str": "hello world"})
BAD_EDIT = ("edit", {"file_path": "f.py", "old_str": "nowhere in this file", "new_str": "x"})
WRITE = ("write", {"file_path": "new.py", "content": "x = 1\n"})
R = lambda n: ("read", {"file_path": f"{n}.py"})
ECHO = lambda n: ("bash", {"command": f"echo probe {n}"})


class Scripted(Beekeeper):
    """Records the schema the model would actually see, per turn, by building
    the real request body — never by reading the harness's private state."""

    def __init__(self, arena, script, verify_cmd=START_RED, **kw):
        super().__init__(str(arena), "the task", verify_cmd=verify_cmd, **kw)
        self.script = list(script)
        self.schemas = []

    def request(self):
        self.schemas.append([t["function"]["name"] for t in self._request_body()["tools"]])
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
    for k in ("BEEKEEPER_PHASE", "BEEKEEPER_PHASE_K", "BEEKEEPER_THINK"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    for n in "ghij":
        (a / f"{n}.py").write_text(f"# {n}\nvalue = '{n}'\n")
    return a


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")


def _records(tmp_path, kind=None):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    recs = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    return [r for r in recs if kind is None or r["kind"] == kind]


def _settings(capsys):
    """the LAST settings line printed: a test may build more than one worker"""
    return [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][-1]


# --- the flag itself ---

def test_off_by_default_the_schema_is_whole_and_the_settings_say_so(arena, tmp_path, capsys):
    bk = Scripted(arena, [EDIT, R("g"), RED_RUN, EDIT])
    bk.run()
    assert all({"edit", "write", "read", "bash", "done"} <= set(s) for s in bk.schemas), bk.schemas
    assert not _records(tmp_path, "phase"), "no phase records when the lever is off"
    assert not [m for m in bk.messages if "no edit yet" in str(m.get("content") or "")]
    line = _settings(capsys)
    assert "phase=off(default)" in line and "phase_k=6(default)" in line, line


def test_k_is_six_by_default_and_declared_from_the_env(arena, monkeypatch, capsys):
    assert Scripted(arena, []).phase_k == 6
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "4")
    bk = Scripted(arena, [])
    assert bk.phase_k == 4 and bk.phase_policy == "on"
    line = _settings(capsys)
    assert "phase=on(env)" in line and "phase_k=4(env)" in line, line


# --- law 1: orient before you commit ---

def test_turn_one_carries_no_edit_or_write(arena, on):
    bk = Scripted(arena, [R("g"), EDIT])
    bk.run()
    assert "edit" not in bk.schemas[0] and "write" not in bk.schemas[0], bk.schemas[0]
    assert {"read", "bash", "done"} <= set(bk.schemas[0]), bk.schemas[0]


def test_the_start_verify_counts_so_turn_two_may_edit(arena, on):
    bk = Scripted(arena, [R("g"), EDIT])
    bk.run()
    assert {"edit", "write"} <= set(bk.schemas[1]), bk.schemas[1]
    assert (arena / "f.py").read_text() == "greeting = 'hello world'\n", "the edit landed on turn 2"


def test_without_a_start_verify_the_model_must_run_one_first(arena, on):
    bk = Scripted(arena, [R("g"), RED_RUN, EDIT], start_verify=False)
    bk.run()
    assert "edit" not in bk.schemas[1] and "write" not in bk.schemas[1], "no verify observed yet"
    assert {"edit", "write"} <= set(bk.schemas[2]), bk.schemas[2]
    assert (arena / "f.py").read_text() == "greeting = 'hello world'\n"


def test_with_no_verify_at_all_only_turn_one_is_gated(arena, on):
    """A gate on an observation that cannot exist would deadlock the run."""
    bk = Scripted(arena, [R("g"), EDIT], verify_cmd=None)
    bk.run()
    assert "edit" not in bk.schemas[0]
    assert {"edit", "write"} <= set(bk.schemas[1]), bk.schemas[1]


# --- law 2: a red verify after an edit re-opens the observation ---

def test_a_red_verify_after_an_edit_withholds_edit_until_a_read_or_bash(arena, on, tmp_path):
    bk = Scripted(arena, [R("g"), EDIT, RED_RUN, R("h"), R("i")])
    bk.run()
    assert "edit" not in bk.schemas[3], bk.schemas[3]
    assert {"write", "read", "bash", "done"} <= set(bk.schemas[3]), "only edit leaves"
    assert "edit" in bk.schemas[4], "a read re-opened it"
    assert bk.edit_withheld_after_red == 1
    assert [r["reason"] for r in _records(tmp_path, "phase") if r["turn"] == 4] == ["edit_after_red"]


def test_a_bash_also_re_opens_edit(arena, on):
    bk = Scripted(arena, [R("g"), EDIT, RED_RUN, ECHO(1), R("i")])
    bk.run()
    assert "edit" not in bk.schemas[3] and "edit" in bk.schemas[4], bk.schemas[3:5]


def test_a_red_verify_that_follows_no_edit_leaves_edit_in_the_schema(arena, on):
    bk = Scripted(arena, [R("g"), RED_RUN, R("h")])
    bk.run()
    assert "edit" in bk.schemas[2], bk.schemas[2]
    assert bk.edit_withheld_after_red == 0 and bk.red_after_edit == 0


def test_a_green_verify_after_an_edit_never_withholds(arena, on):
    bk = Scripted(arena, [R("g"), EDIT, GREEN_RUN, R("h")])
    bk.run()
    assert "edit" in bk.schemas[3], bk.schemas[3]
    assert bk.edit_withheld_after_red == 0


# --- law 3: the first edit, forced by turn k ---

def test_turn_k_withholds_read_and_bash_and_says_so_in_one_line(arena, on, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "4")
    bk = Scripted(arena, [R("g"), R("h"), R("i"), EDIT])
    bk.run()
    assert {"read", "bash"} <= set(bk.schemas[2]), "turn 3 is still free"
    s4 = set(bk.schemas[3])
    assert "read" not in s4 and "bash" not in s4, bk.schemas[3]
    assert {"edit", "write", "done"} <= s4, bk.schemas[3]
    notes = [str(m["content"]) for m in bk.messages
             if m.get("role") == "user" and "no edit yet" in str(m.get("content") or "")]
    assert len(notes) == 1 and "\n" not in notes[0], notes
    assert bk.first_edit_turn == 4 and bk.forced_edit is True
    assert [r["turn"] for r in _records(tmp_path, "phase") if "force_first_edit" in r["reason"]] == [4]


def test_an_edit_before_k_is_never_forced(arena, on, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "4")
    bk = Scripted(arena, [R("g"), EDIT, R("h"), R("i"), R("j")])
    bk.run()
    assert {"read", "bash"} <= set(bk.schemas[3]), "an edit already landed: nothing to force"
    assert bk.first_edit_turn == 2 and bk.forced_edit is False and bk.forced_turns == 0
    assert not [m for m in bk.messages if "no edit yet" in str(m.get("content") or "")]


def test_a_failed_edit_does_not_count_as_an_edit(arena, on, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "3")
    bk = Scripted(arena, [R("g"), R("h"), BAD_EDIT, EDIT])
    bk.run()
    assert "read" not in bk.schemas[3], "turn 4 still forces: the failed edit changed nothing"
    assert bk.first_edit_turn == 4 and bk.forced_edit is True


def test_the_forcing_never_leaves_the_model_with_nothing_to_do(arena, on, monkeypatch):
    """k arrives while edit and write are themselves withheld (no verify has
    been observed): the forcing yields — a schema of `done` alone is not a
    phase, it is a wall."""
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "2")
    bk = Scripted(arena, [R("g"), R("h"), R("i")], start_verify=False)
    bk.run()
    assert {"read", "bash"} <= set(bk.schemas[1]), bk.schemas[1]
    assert bk.forced_turns == 0


# --- law 4: the ledger ---

def test_every_transition_is_a_record_and_only_transitions_are(arena, on, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "4")
    bk = Scripted(arena, [R("g"), R("h"), R("i"), EDIT, RED_RUN, R("j")])
    bk.run()
    ph = _records(tmp_path, "phase")
    assert ph, "no phase records"
    assert ph[0]["turn"] == 1 and ph[0]["reason"] == "turn_one"
    assert "edit" not in ph[0]["schema"] and "read" in ph[0]["schema"], ph[0]
    assert [r["reason"] for r in ph] == ["turn_one", "open", "force_first_edit", "open",
                                         "edit_after_red", "open"], ph
    assert len(ph) < bk.turn, "a record per transition, not a record per turn"
    for r in ph:
        assert set(r) >= {"kind", "turn", "schema", "reason"} and isinstance(r["schema"], list)


def test_the_end_record_carries_the_opportunity_counts(arena, on, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "4")
    bk = Scripted(arena, [R("g"), EDIT, RED_RUN, R("h")])
    bk.run()
    end = _records(tmp_path, "end")[-1]
    assert end["first_edit_turn"] == 2 and end["forced_edit"] is False
    assert end["edit_withheld_after_red"] == 1 and end["red_after_edit"] == 1
    assert end["forced_turns"] == 0 and end["phase_policy"] == "on" and end["phase_k"] == 4


def test_the_control_arm_reports_its_opportunities_too(arena, tmp_path):
    """M-04: the counts are the yardstick, so the flags-off arm records them —
    the gate is simply never reached (`edit_withheld_after_red` 0 against a
    real `red_after_edit`), which is measured, not inert."""
    bk = Scripted(arena, [R("g"), EDIT, RED_RUN, R("h")])
    bk.run()
    end = _records(tmp_path, "end")[-1]
    assert end["phase_policy"] == "off" and end["first_edit_turn"] == 2
    assert end["red_after_edit"] == 1 and end["edit_withheld_after_red"] == 0
    assert end["forced_edit"] is False and end["forced_turns"] == 0


def test_never_edited_is_reported_as_none(arena, on):
    bk = Scripted(arena, [R("g"), R("h")])
    bk.run()
    assert bk.first_edit_turn is None and bk.forced_edit is False


def test_a_write_is_a_first_edit_too(arena, on, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "3")
    bk = Scripted(arena, [R("g"), R("h"), WRITE])
    bk.run()
    assert (arena / "new.py").exists()
    assert bk.first_edit_turn == 3 and bk.forced_edit is True
