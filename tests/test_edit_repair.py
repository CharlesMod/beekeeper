"""H-34, the edit repair ladder — and the ledger field that made it readable.

MEASURED first (2026-09-06, pool v2's seven finished spend ledgers + their
transcripts): 254 edit calls, 111 applied, 76 `old_str not found` — 30% of
every edit call refused, 18-42% per arm, and 0 ambiguous. Edits are the scarce
resource in this harness (294 edit turns against 5,640 bash and 1,820 read;
the phase gate lifts tasks-that-edit-at-all from 13/28 to 24/28), so a refused
edit is not one call wasted, it is one of the run's handful of commitments
thrown away. None of it was visible in the ledger: a `t_edit` `fail('args',
"old_str not found")` is not a refusal and was recorded nowhere.

Two things, and only one of them is a lever:

  1. THE LEDGER (not a lever — it measures under BOTH settings): every edit
     turn's record carries `applied`, `repair` and `fail_class`, so the next
     ring reads the apply-failure rate off the ledger instead of grepping
     transcripts for a message string.

  2. THE REPAIR LADDER (the lever, BEEKEEPER_EDIT_REPAIR=on, default off):
     when the exact match fails, four normalisations are tried IN ORDER and
     the first UNIQUE hit wins — trailing whitespace, leading indentation,
     internal whitespace runs, a single-line fragment. Every step is an EXACT
     match after a declared normalisation. No similarity score may ever pick
     a block to edit: it can pick the wrong one, and an edit applied to the
     wrong block is worse than a refusal. Similarity is allowed to say only
     WHERE to look, in a one-line hint on the refusal.

The flag off is today's worker, byte for byte: the same edits apply, the same
edits refuse, with the same message.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402

NOT_FOUND = ("old_str not found — copy the exact lines from a fresh read, "
             "without the N| line-number prefixes")

SRC = ("def total(a, b):\n"
       "    if a is None:\n"
       "        return b\n"
       "    return a + b\n")


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    for k in ("BEEKEEPER_EDIT_REPAIR", "BEEKEEPER_BOARD", "BEEKEEPER_PHASE",
              "BEEKEEPER_ALT", "BEEKEEPER_THINK", "BEEKEEPER_RESTART", "BEEKEEPER_CREATE"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "m.py").write_text(SRC)
    return a


def bk_on(arena, monkeypatch, **kw):
    monkeypatch.setenv("BEEKEEPER_EDIT_REPAIR", "on")
    return Beekeeper(str(arena), "t", **kw)


def bk_off(arena, **kw):
    return Beekeeper(str(arena), "t", **kw)


# ---------------------------------------------------------------- the ladder

def test_trailing_whitespace_is_repaired_as_ws(arena, monkeypatch):
    """The model copies a line and adds trailing spaces the file does not have."""
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "    if a is None:   \n        return b", "    if a is None:\n        return 0")
    assert not out.startswith("ERROR"), out
    assert "return 0" in (arena / "m.py").read_text()
    assert bk.last_edit_record["repair"] == "ws", bk.last_edit_record
    assert bk.last_edit_record["applied"] is True


def test_leading_indentation_is_repaired_and_new_str_reindented(arena, monkeypatch):
    """The model quotes a block de-indented; new_str comes back at the file's
    own indentation, not the model's."""
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "if a is None:\n    return b", "if a is None:\n    return 0")
    assert not out.startswith("ERROR"), out
    assert "    if a is None:\n        return 0\n" in (arena / "m.py").read_text()
    assert bk.last_edit_record["repair"] == "indent", bk.last_edit_record


def test_internal_whitespace_runs_are_repaired_as_inner_ws(arena, monkeypatch):
    (arena / "m.py").write_text("def total(a, b):\n    return a  +   b\n")
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "    return a + b", "    return a - b")
    assert not out.startswith("ERROR"), out
    assert (arena / "m.py").read_text() == "def total(a, b):\n    return a - b\n"
    assert bk.last_edit_record["repair"] == "inner-ws", bk.last_edit_record


def test_a_single_line_fragment_unique_after_stripping_is_repaired_as_line(arena, monkeypatch):
    """A mid-line fragment the model padded with whitespace: unique as an exact
    substring once stripped, so the replacement is unambiguous."""
    (arena / "m.py").write_text("def total(a, b):\n    return helper(a) + b  # keep\n")
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "  helper(a)  ", "  helper(b)  ")
    assert not out.startswith("ERROR"), out
    assert (arena / "m.py").read_text() == "def total(a, b):\n    return helper(b) + b  # keep\n"
    assert bk.last_edit_record["repair"] == "line", bk.last_edit_record


def test_the_ladder_stops_at_the_first_unique_hit(arena, monkeypatch):
    """An old_str that only trailing whitespace separates from the file is `ws`,
    never `indent` — the class names which normalisation was needed, and that
    is the measurement."""
    bk = bk_on(arena, monkeypatch)
    bk.t_edit("m.py", "    return a + b  ", "    return a * b")
    assert bk.last_edit_record["repair"] == "ws", bk.last_edit_record


# ------------------------------------------------------------- what it refuses

def test_an_ambiguous_content_match_is_refused_not_guessed(arena, monkeypatch):
    """Two blocks that differ only in indentation: the repair must refuse, not
    pick one. An edit applied to the wrong block is worse than a refusal."""
    (arena / "m.py").write_text("if x:\n    go()\nwhile y:\n  if x:\n      go()\n")
    bk = bk_on(arena, monkeypatch)
    before = (arena / "m.py").read_text()
    out = bk.t_edit("m.py", "        if x:\n            go()", "        if x:\n            stop()")
    assert out.startswith("ERROR"), out
    assert NOT_FOUND in out
    assert (arena / "m.py").read_text() == before, "an ambiguous match must leave the tree untouched"
    assert bk.last_edit_record["applied"] is False
    assert bk.last_edit_record["fail_class"] == "not_found"


def test_no_similarity_score_may_apply_an_edit(arena, monkeypatch):
    """A near-miss that no normalisation makes exact is REFUSED — a fuzzy match
    could land on the wrong block."""
    bk = bk_on(arena, monkeypatch)
    before = (arena / "m.py").read_text()
    out = bk.t_edit("m.py", "    if a is not None:\n        return c", "    return 0")
    assert out.startswith("ERROR") and NOT_FOUND in out, out
    assert (arena / "m.py").read_text() == before


def test_exactly_one_near_miss_earns_a_line_range_hint(arena, monkeypatch):
    """Similarity may say WHERE to look; it may not decide what to edit."""
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "    if a is not None:\n        return c", "    return 0")
    assert NOT_FOUND in out, out
    assert "lines 2-3" in out, out
    assert "m.py" in out


def test_more_than_one_occurrence_is_still_ambiguous(arena, monkeypatch):
    (arena / "m.py").write_text("go()\ngo()\n")
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "go()", "stop()")
    assert "occurs 2 times" in out, out
    assert bk.last_edit_record["fail_class"] == "ambiguous", bk.last_edit_record


def test_the_numeric_only_gate_is_named_in_the_record(arena, monkeypatch):
    (arena / "m.py").write_text("LIMIT = 5\n")
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "LIMIT = 5", "LIMIT = 7")
    assert out.startswith("ERROR"), out
    assert bk.last_edit_record["fail_class"] == "numeric_only", bk.last_edit_record
    assert bk.last_edit_record["applied"] is False


def test_a_path_outside_the_arena_is_named_in_the_record(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("/etc/nope-not-here.conf", "a", "b")
    assert out.startswith("ERROR"), out
    assert bk.last_edit_record["fail_class"] == "outside", bk.last_edit_record


# ------------------------------------------------------------------ flag off

def test_flag_off_refuses_what_the_ladder_would_repair_with_todays_message(arena):
    """The control arm is today's worker, byte for byte."""
    (arena / "m.py").write_text("def total(a, b):\n    return a  +   b\n")
    before = (arena / "m.py").read_text()
    bk = bk_off(arena)
    out = bk.t_edit("m.py", "    return a + b", "    return a - b")
    assert out == f"ERROR[args]: {NOT_FOUND}", out
    assert "closest block" not in out, "the hint is the lever's; the control message is unchanged"
    assert (arena / "m.py").read_text() == before


def test_flag_off_keeps_the_pre_existing_grace(arena):
    """The whitespace-corrected content match shipped before H-34 and is not
    the lever — it keeps working with the flag off, and is labelled `indent`."""
    bk = bk_off(arena)
    out = bk.t_edit("m.py", "if a is None:\n    return b", "if a is None:\n    return 0")
    assert not out.startswith("ERROR"), out
    assert "    if a is None:\n        return 0\n" in (arena / "m.py").read_text()
    assert bk.last_edit_record["repair"] == "indent", bk.last_edit_record


def test_flag_off_keeps_the_line_number_prefix_strip(arena):
    bk = bk_off(arena)
    out = bk.t_edit("m.py", "  3|        return b", "  3|        return 0")
    assert not out.startswith("ERROR"), out
    assert "        return 0\n" in (arena / "m.py").read_text()
    assert bk.last_edit_record["repair"] == "prefix", bk.last_edit_record


def test_an_exact_match_repairs_nothing_under_either_flag(arena, monkeypatch):
    for bk in (bk_off(arena), bk_on(arena, monkeypatch)):
        (arena / "m.py").write_text(SRC)
        out = bk.t_edit("m.py", "    return a + b", "    return a - b")
        assert not out.startswith("ERROR"), out
        assert bk.last_edit_record == {"applied": True, "repair": "none", "fail_class": "none"}


def test_the_settings_line_names_the_flag(arena, monkeypatch, capsys):
    Beekeeper(str(arena), "t")
    assert "edit_repair=off(default)" in capsys.readouterr().out
    monkeypatch.setenv("BEEKEEPER_EDIT_REPAIR", "on")
    Beekeeper(str(arena), "t")
    assert "edit_repair=on(env)" in capsys.readouterr().out


# ------------------------------------------------------------------ the ledger

VERIFY = ("sh -c 'if test -f fixed; then echo \"1 passed in 0.1s\"; exit 0; else "
          "echo \"1 failed in 0.1s\"; exit 1; fi' -- tests/t.py::a")


class Scripted(Beekeeper):
    def __init__(self, arena, task, script, **kw):
        super().__init__(str(arena), task, **kw)
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


def records(tmp_path):
    return [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]


SCRIPT = [("edit", {"file_path": "m.py", "old_str": "    return a + b",
                    "new_str": "    return a - b"}),                       # exact
          ("edit", {"file_path": "m.py", "old_str": "if a is None:\n    return b",
                    "new_str": "if a is None:\n    return 0"}),            # indent
          ("edit", {"file_path": "m.py", "old_str": "    return nothing_like_this",
                    "new_str": "    return 1"})]                           # not_found


@pytest.mark.parametrize("flag", ["off", "on"])
def test_every_edit_turn_carries_applied_repair_and_fail_class(arena, tmp_path, monkeypatch, flag):
    """The ledger measuring is NOT the lever: both settings record it, or the
    control arm cannot be compared with the treatment."""
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    if flag == "on":
        monkeypatch.setenv("BEEKEEPER_EDIT_REPAIR", "on")
    Scripted(arena, "the task", SCRIPT).run()
    turns = [r for r in records(tmp_path) if r.get("kind") == "turn" and r.get("action") == "edit"]
    assert len(turns) == 3, turns
    assert [t["applied"] for t in turns] == [True, True, False], turns
    assert [t["repair"] for t in turns] == ["none", "indent", "none"], turns
    assert [t["fail_class"] for t in turns] == ["none", "none", "not_found"], turns


def test_the_end_record_counts_edit_repairs_and_edit_failures(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    Scripted(arena, "the task", SCRIPT).run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert {"edit_calls", "edit_repairs", "edit_failures"} <= set(o), sorted(o)
    assert o["edit_calls"] == 3 and o["edit_repairs"] == 1 and o["edit_failures"] == 1, o


def test_zero_is_a_measurement_a_run_with_no_edit_still_carries_the_counts(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    Scripted(arena, "the task", [("bash", {"command": "echo hi"})]).run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert o["edit_calls"] == 0 and o["edit_repairs"] == 0 and o["edit_failures"] == 0, o
