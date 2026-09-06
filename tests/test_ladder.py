"""H-08, the test ladder: the board's rows are RUNGS — one per named test,
green or red as the last verify's own per-test lines reported it — so the
model sees WHICH test moved, not only how many are left. H-01 seeds its rows
from the ids in the verify COMMAND, so a verify that names a file (the common
shape in the public ring) renders no board at all and a run that greens one of
four tests learns nothing. Laws (BEEKEEPER_LADDER=on; off by default, the
transcript byte-identical when off):

  - off by default; `BEEKEEPER_LADDER=on` declares it and the settings line
    names it with its source;
  - a rung is discovered from pytest's per-test lines in the verify OUTPUT —
    the short-summary form (`FAILED tests/t.py::a - boom`, `PASSED …`) and
    the verbose form (`tests/t.py::b PASSED [ 50%]`) alike;
  - a discovered rung carries the state the output gave it: PASSED green,
    FAILED/ERROR red;
  - the rungs stay bounded to the files the verify command names — a run that
    happens to print another file's tests does not grow the board;
  - the board's budget is spent on the FAILING rungs first: with more rows
    than fit, the red ones are the ones the model sees;
  - M-02: a run that could not collect discovers no rungs;
  - with the flag off nothing is discovered, nothing is reordered, and the
    transcript is byte-identical.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

IDS = "tests/t.py::a tests/t.py::b"
# H-01's shape: the command names the ids, and both are red at the start
RED2 = ("sh -c 'echo \"FAILED tests/t.py::a - boom\"; echo \"FAILED tests/t.py::b - boom\"; "
        "echo \"2 failed in 0.1s\"; exit 1' -- " + IDS)
# the public ring's shape: the command names the FILE, so H-01 has no ids to
# seed with. The ids are assembled in the shell so the command STRING carries
# none of them either — a literal `tests/t.py::a` anywhere in the command is a
# row H-01 would seed, and the test would prove nothing about discovery.
FILE_CMD = ("sh -c 'p=tests/t.py; echo \"$p::a FAILED [ 33%]\"; echo \"$p::b PASSED [ 66%]\"; "
            "echo \"$p::c FAILED [100%]\"; echo \"2 failed, 1 passed in 0.1s\"; exit 1' "
            "-- pytest tests/t.py")
# an uncollected run as the verify itself
NOCOLLECT = ("sh -c 'echo \"usage: pytest: error: unrecognized arguments\"; exit 4' "
             "-- pytest tests/t.py")
# a verify run by the model that names a third id in the same file, in the
# short-summary form, and reports the earlier one green
V_THIRD = ("bash", {"command": "sh -c 'echo \"PASSED tests/t.py::a\"; "
                               "echo \"FAILED tests/t.py::b - boom\"; "
                               "echo \"ERROR tests/t.py::c - setup\"; "
                               "echo \"1 failed, 1 error, 1 passed in 0.2s\"; exit 1' -- pytest tests/t.py " + IDS})
# a run whose output names a test in a file the verify command never mentions
V_FOREIGN = ("bash", {"command": "sh -c 'echo \"FAILED tests/other.py::z - boom\"; "
                                 "echo \"FAILED tests/t.py::a - boom\"; "
                                 "echo \"2 failed in 0.2s\"; exit 1' -- pytest " + IDS})
# both known ids red again: nothing to discover, nothing to reorder
V_SAME = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - again\"; "
                              "echo \"FAILED tests/t.py::b - again\"; "
                              "echo \"2 failed in 0.2s\"; exit 1' -- pytest " + IDS})


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
    monkeypatch.setenv("BEEKEEPER_LADDER", "on")
    for k in ("BEEKEEPER_TREND", "BEEKEEPER_RESTART", "BEEKEEPER_CREATE", "BEEKEEPER_THINK",
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


def records(tmp_path):
    return [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]


def settings(capsys):
    return [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]


# --- the flag ---

def test_off_by_default_and_named_in_the_settings_line(arena, monkeypatch, capsys):
    monkeypatch.delenv("BEEKEEPER_LADDER")
    bk = Scripted(arena, [("bash", {"command": "echo hi"})], verify_cmd=FILE_CMD)
    bk.run()
    assert bk.ladder_policy == "off"
    assert "ladder=off(default)" in settings(capsys)
    assert bk.board_rows == {}, "H-01 seeds from the command's ids; this command names none"
    assert board(bk) == ""


def test_on_is_declared_in_the_settings_line(arena, capsys):
    Scripted(arena, [])
    assert "ladder=on(env)" in settings(capsys)


# --- the rungs ---

def test_a_file_only_verify_grows_rungs_from_the_verbose_lines(arena):
    """The public ring's shape: the command names a file, the output names
    the tests. H-01 alone renders nothing here."""
    bk = Scripted(arena, [("bash", {"command": "echo hi"})], verify_cmd=FILE_CMD)
    bk.run()
    text = board(bk)
    assert "✗ tests/t.py::a" in text and "✓ tests/t.py::b" in text and "✗ tests/t.py::c" in text, text
    assert set(bk.board_rows) == {"tests/t.py::a", "tests/t.py::b", "tests/t.py::c"}, bk.board_rows


def test_the_short_summary_form_grows_a_rung_and_carries_its_state(arena):
    bk = Scripted(arena, [V_THIRD])
    bk.run()
    text = board(bk)
    assert "✓ tests/t.py::a" in text, text          # PASSED tests/t.py::a
    assert "✗ tests/t.py::b" in text, text          # FAILED
    assert "✗ tests/t.py::c" in text, text          # ERROR, and the id was never in the command
    assert "tests/t.py::c" not in RED2


def test_the_rungs_stay_inside_the_files_the_verify_names(arena):
    bk = Scripted(arena, [V_FOREIGN])
    bk.run()
    assert "tests/other.py::z" not in board(bk), board(bk)
    assert set(bk.board_rows) == {"tests/t.py::a", "tests/t.py::b"}, bk.board_rows


def test_an_uncollected_run_discovers_no_rungs(arena):
    """M-02: exit 4 proves nothing — not even which tests exist."""
    bk = Scripted(arena, [("bash", {"command": "echo hi"})], verify_cmd=NOCOLLECT)
    bk.run()
    assert bk.board_rows == {}, bk.board_rows
    assert board(bk) == ""


def test_the_ladder_writes_only_where_the_board_gave_it_one(arena, monkeypatch):
    """Control: the rungs ARE board rows. With H-01 off there is nothing to
    write on, and a board=off arm's row count stays a reading of H-01."""
    monkeypatch.delenv("BEEKEEPER_BOARD")
    bk = Scripted(arena, [V_THIRD], verify_cmd=FILE_CMD)
    bk.run()
    assert bk.board_rows == {} and bk.ladder_discovered == 0, bk.board_rows


# --- the budget ---

def test_the_failing_rungs_are_rendered_first_inside_the_budget(arena):
    """23 rungs, the three red ones last in the output: insertion order would
    spend the whole budget on green."""
    passing = "; ".join(f'echo "$p::p{i:02d} PASSED [ {i}%]"' for i in range(20))
    failing = "; ".join(f'echo "$p::x{j} FAILED [ 9{j}%]"' for j in range(3))
    cmd = (f"sh -c 'p=tests/t.py; {passing}; {failing}; echo \"3 failed, 20 passed in 1.0s\"; exit 1' "
           "-- pytest tests/t.py")
    bk = Scripted(arena, [("bash", {"command": "echo hi"})], verify_cmd=cmd)
    bk.run()
    text = board(bk)
    assert len(bk.board_rows) == 23, sorted(bk.board_rows)
    for j in range(3):
        assert f"✗ tests/t.py::x{j}" in text, text
    assert len(text) <= 1200 and "more" in text, (len(text), text)


def test_the_end_record_carries_the_ladders_opportunity_counts(arena, tmp_path):
    """M-04: a lever that discovered nothing is unmeasured, not inert."""
    bk = Scripted(arena, [V_THIRD])
    bk.run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert {"ladder_rungs", "ladder_discovered"} <= set(o), sorted(o)
    assert o["ladder_rungs"] == 3 and o["ladder_discovered"] == 1, o


# --- the blast radius ---

def test_off_the_transcript_is_byte_identical(arena, monkeypatch):
    """With nothing to discover and nothing to reorder, on and off are the
    same run: the flag's whole effect is rungs and their order."""
    script = [V_SAME, ("bash", {"command": "echo hi"})]
    monkeypatch.delenv("BEEKEEPER_LADDER")
    off = Scripted(arena, list(script))
    off.run()
    monkeypatch.setenv("BEEKEEPER_LADDER", "on")
    on = Scripted(arena, list(script))
    on.run()
    assert off.messages == on.messages
    assert board(off).startswith("[Board")


def test_off_nothing_is_discovered_and_nothing_reordered(arena, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_LADDER")
    bk = Scripted(arena, [V_THIRD])
    bk.run()
    assert set(bk.board_rows) == {"tests/t.py::a", "tests/t.py::b"}, bk.board_rows
    assert "tests/t.py::c" not in board(bk), board(bk)
