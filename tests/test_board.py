"""H-01, the board: a harness-owned ledger the model sees every turn — one
row per visible test, flipped by VERIFICATION alone, rebuilt each turn
rather than accumulated, pinned against compaction, under a fixed budget.
The review's finding: model-written todo lists are theatre (Claude Code
hides them now); a ledger the court writes from the verify set is not.
Laws:
  - off by default; `BEEKEEPER_BOARD=on` declares it, the settings line
    names it;
  - the rows are the visible test ids parsed from the verify command; each
    row is red, green or unmeasured, with the turn it was last observed;
  - only a verify observation flips a row — the start verify, a verify-like
    bash call's output, the verify at done; a model's prose never does;
  - one board message, rebuilt every turn in place, pinned, surviving
    compaction; at most ~1200 characters, extra rows folded into a count.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

IDS = "tests/t.py::a tests/t.py::b tests/t.py::c"
RED2 = ("sh -c 'echo \"FAILED tests/t.py::a - boom\"; echo \"FAILED tests/t.py::b - boom\"; "
        "echo \"2 failed, 1 passed in 0.1s\"; exit 1' -- " + IDS)
GREEN = "sh -c 'echo \"3 passed in 0.1s\"; exit 0' -- python -m pytest -q " + IDS   # the model re-runs the tests


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify_cmd=RED2):
        super().__init__(str(arena), "the task", verify_cmd=verify_cmd)
        self.script = list(script)

    def request(self):
        if not self.script:
            return {"message": {"content": ""}, "finish_reason": "stop"}
        item = self.script.pop(0)
        if isinstance(item, str):                       # prose, no tool call
            return {"message": {"content": item}, "finish_reason": "stop"}
        name, args = item
        return {"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls"}


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_BOARD", "on")
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def _board(bk):
    boards = [m for m in bk.messages if m.get("role") == "user" and str(m.get("content", "")).startswith("[Board")]
    return boards


def test_off_by_default_no_board_and_settings_say_so(arena, monkeypatch, capsys):
    monkeypatch.delenv("BEEKEEPER_BOARD")
    bk = Scripted(arena, [("bash", {"command": "echo hi"})])
    bk.run()
    assert not _board(bk)
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "board=off(default)" in line, line


def test_the_start_verify_seeds_the_rows(arena, capsys):
    bk = Scripted(arena, [("bash", {"command": "echo hi"})])
    bk.run()
    b = _board(bk)
    assert len(b) == 1, b
    text = b[0]["content"]
    assert "✗ tests/t.py::a" in text and "✗ tests/t.py::b" in text and "✓ tests/t.py::c" in text, text
    assert bk.messages.index(b[0]) in bk.pin_idx
    assert "board=on(env)" in [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]


def test_only_a_verify_observation_flips_a_row(arena):
    bk = Scripted(arena, ["All tests pass now, done.", ("bash", {"command": "echo all tests pass"}),
                          ("bash", {"command": GREEN})])
    bk.run()
    text = _board(bk)[0]["content"]
    assert "✓ tests/t.py::a" in text and "✓ tests/t.py::b" in text, text
    assert [f for f in bk.board_flips if f[0] == 3] == [(3, "tests/t.py::a"), (3, "tests/t.py::b")], bk.board_flips
    assert (0, "tests/t.py::c") in bk.board_flips, "the start verify measured c green: unmeasured -> green is a flip"


def test_prose_never_flips_and_the_board_is_one_message_rebuilt_in_place(arena):
    bk = Scripted(arena, ["I fixed it, all green.", ("bash", {"command": "echo x"}),
                          ("bash", {"command": "echo y"}), ("bash", {"command": "echo z"})])
    bk.run()
    b = _board(bk)
    assert len(b) == 1
    text = b[0]["content"]
    assert text.startswith("[Board t") and int(text[8:].split()[0]) >= 4, text   # rebuilt each turn
    assert "✗ tests/t.py::a — red (start)" in text and "✗ tests/t.py::b — red (start)" in text, text


def test_the_board_survives_compaction_and_stays_pinned(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "600")   # tiny: compaction fires
    big = "x" * 900
    bk = Scripted(arena, [("bash", {"command": f"echo {big}"}), ("bash", {"command": f"echo {big}1"}),
                          ("bash", {"command": f"echo {big}2"}), ("bash", {"command": f"echo {big}3"}),
                          ("bash", {"command": f"echo {big}4"})])
    bk.run()
    assert bk.compactions >= 1, "the fixture must compact"
    b = _board(bk)
    assert len(b) == 1 and bk.messages.index(b[0]) in bk.pin_idx
    assert "✗ tests/t.py::a" in b[0]["content"]


def test_the_board_fits_its_budget(arena):
    ids = " ".join(f"tests/t.py::t{i}" for i in range(40))
    cmd = "sh -c 'echo \"FAILED tests/t.py::t1 - boom\"; exit 1' -- " + ids
    bk = Scripted(arena, [("bash", {"command": "echo hi"})], verify_cmd=cmd)
    bk.run()
    text = _board(bk)[0]["content"]
    assert len(text) <= 1200 and "more" in text, (len(text), text[-100:])


def test_a_verify_that_could_not_collect_leaves_rows_unmeasured(arena):
    """M-02: exit 4 with 'not found' proves nothing — rows keep their last
    state instead of flipping red (B3 found the exit-code regex never matched)."""
    # exit 4 with NO signature phrase: only the exit code says the run measured nothing;
    # the old regex (a doubled backslash) never matched, read the exit as red, found no
    # FAILED lines and greened every row
    cannot = "sh -c 'echo usage: pytest: error: unrecognized arguments; exit 4' -- pytest " + IDS
    bk = Scripted(arena, [("bash", {"command": cannot})])      # rows start red from the start verify
    bk.run()
    text = _board(bk)[0]["content"]
    assert "✗ tests/t.py::a" in text and "✗ tests/t.py::b" in text and "✓ tests/t.py::c" in text, text
