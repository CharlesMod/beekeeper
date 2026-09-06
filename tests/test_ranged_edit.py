"""H-34, redirected: an edit addressed by LINE RANGE, so the failing edit never
has to be typed at all.

MEASURED (2026-09-06, the screen, ops/ring/REPORT-pub2.md §"The screen"):
25-30% of every edit call the model makes is refused, and the repair ladder
built for it measured INERT for the right reason — of six `not_found`
failures, none matched any rung and not one near-miss hint fired (a hint needs
one window at similarity >= 0.7). The `old_str` the model asks to replace is
not a near-miss of anything in the file: it is not in the file at all. Repair
cannot reach that. Only prevention can.

The prevention is addressing. The read tool already returns `N|`-numbered
lines and, under BEEKEEPER_SYMBOLMAP=on, a ranged read. A model that has just
read lines 40-60 can then say "replace lines 47-49" without retyping one
character of them — the copy step, which is where the failure lives, is gone.

Laws:
  - off by default; `BEEKEEPER_RANGED_EDIT=on` declares it and is named on the
    settings line with its source, as BEEKEEPER_SYMBOLMAP/BOARD/PHASE are;
  - flag off, the edit tool's schema, its output and its refusals are
    unchanged, byte for byte, and a range is an argument error;
  - the schema and the prompt agree PER TURN (B9's law: the lean prompt is
    rendered from the turn's own array) — the ranged form is named in both or
    in neither;
  - a range is validated against the file's CURRENT content: a stale range,
    left over from before another edit shortened the file, is refused BY NAME
    with the current line count and how to re-read. It is never clamped: a
    clamped range edits lines the model never saw;
  - the laws that ride the text form ride the range form — the numeric-literal
    gate and the syntax guard are not bypassed by changing how you point;
  - the ledger's turn record carries `addressing: text|range` on every edit
    turn under BOTH
    settings, so the next ring can compare the two forms' apply-failure rates
    directly. Measuring is not the lever.
"""
import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402

SRC = ("def total(a, b):\n"
       "    if a is None:\n"
       "        return b\n"
       "    return a + b\n")


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    for k in ("BEEKEEPER_RANGED_EDIT", "BEEKEEPER_EDIT_REPAIR", "BEEKEEPER_SYMBOLMAP",
              "BEEKEEPER_BOARD", "BEEKEEPER_PHASE", "BEEKEEPER_ALT", "BEEKEEPER_THINK",
              "BEEKEEPER_RESTART", "BEEKEEPER_CREATE", "BEEKEEPER_RULES"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "m.py").write_text(SRC)
    (a / "notes.txt").write_text("alpha\nbeta\ngamma\ndelta\n")
    return a


def bk_on(arena, monkeypatch, **kw):
    monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    return Beekeeper(str(arena), "t", **kw)


def bk_off(arena, **kw):
    return Beekeeper(str(arena), "t", **kw)


def edit_schema(bk):
    return [t for t in bk.tools() if t["function"]["name"] == "edit"][0]["function"]


# ---------------------------------------------------------- the flag is declared

def test_off_by_default_and_named_on_the_settings_line(arena, capsys, monkeypatch):
    bk = Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "ranged_edit=off(default)" in line, line
    assert bk.ranged_edit_policy == "off"
    monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    bk = Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "ranged_edit=on(env)" in line, line
    assert bk.ranged_edit_policy == "on"


# ---------------------------------------------------------------- the flag OFF

def test_flag_off_the_edit_schema_has_no_range(arena):
    props = edit_schema(bk_off(arena))["parameters"]["properties"]
    assert set(props) == {"file_path", "old_str", "new_str"}, props


def test_flag_off_a_range_is_refused_as_an_argument_error_and_costs_no_edit_call(arena):
    bk = bk_off(arena)
    r = bk.t_edit("m.py", new_str="x", start_line=1, end_line=2)
    assert r.startswith("ERROR[args]"), r
    assert (arena / "m.py").read_text() == SRC
    assert bk.edit_calls == 0, "a call the schema never offered is not an edit call"


def test_flag_off_the_text_form_is_byte_identical(arena, monkeypatch):
    """The same edits apply and the same edits refuse, with the same words."""
    on, off = bk_on(arena, monkeypatch), bk_off(arena)
    monkeypatch.delenv("BEEKEEPER_RANGED_EDIT")
    for bk in (off, on):
        (arena / "m.py").write_text(SRC)
        ok = bk.t_edit("m.py", "    return a + b", "    return a - b")
        assert ok == "OK: replaced 1 occurrence", ok
        assert (arena / "m.py").read_text().endswith("    return a - b\n")
        bad = bk.t_edit("m.py", "    return nothing_like_this", "    return 1")
        assert bad.startswith("ERROR[args]: old_str not found"), bad
        assert bk.last_edit_record["fail_class"] == "not_found", bk.last_edit_record


# ----------------------------------------------------------------- the schema

def test_the_edit_schema_declares_the_range_when_on(arena, monkeypatch):
    fn = edit_schema(bk_on(arena, monkeypatch))
    assert set(fn["parameters"]["properties"]) == {
        "file_path", "old_str", "new_str", "start_line", "end_line"}
    assert fn["parameters"]["required"] == ["file_path", "new_str"], fn["parameters"]["required"]
    assert "start_line" in fn["description"]
    # the module-level schema is never mutated: a flag-off worker in the same
    # process must see the shipped tool unchanged
    base = [t for t in beekeeper.TOOLS if t["function"]["name"] == "edit"][0]
    assert set(base["function"]["parameters"]["properties"]) == {"file_path", "old_str", "new_str"}
    assert base["function"]["parameters"]["required"] == ["file_path", "old_str", "new_str"]


def test_the_prompt_and_the_schema_agree_per_turn(arena, monkeypatch):
    """B9's law. The lean prompt's tool line is rendered from the turn's own
    array, so the ranged form is named in both or in neither."""
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    off = bk_off(arena)
    off_prompt = beekeeper.system_prompt("lean", "pytest", off.tools())
    assert "start_line" not in off_prompt, off_prompt
    on = bk_on(arena, monkeypatch)
    on_prompt = beekeeper.system_prompt("lean", "pytest", on.tools())
    assert "start_line" in on_prompt, on_prompt
    assert "edit" in on_prompt


def test_the_turn_body_carries_the_ranged_schema_and_the_matching_prompt(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    bk = bk_on(arena, monkeypatch)
    body = bk._request_body()
    fn = [t for t in body["tools"] if t["function"]["name"] == "edit"][0]["function"]
    assert "start_line" in fn["parameters"]["properties"]
    assert "start_line" in body["messages"][0]["content"]


def test_the_full_prompt_is_untouched_by_the_flag(arena, monkeypatch):
    assert beekeeper.system_prompt("full", "pytest", bk_on(arena, monkeypatch).tools()) == beekeeper.SYSTEM


# ------------------------------------------------------------ addressing by range

def test_a_range_edit_replaces_exactly_those_lines(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", new_str="        return b or 0", start_line=3, end_line=3)
    assert not out.startswith("ERROR"), out
    assert (arena / "m.py").read_text() == ("def total(a, b):\n"
                                            "    if a is None:\n"
                                            "        return b or 0\n"
                                            "    return a + b\n")
    assert bk.last_edit_record == {"applied": True, "repair": "none", "fail_class": "none"}
    assert bk.last_addressing == "range"


def test_a_multi_line_range_replaces_the_whole_block(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", new_str="    if a is None:\n        return 0",
                    start_line=2, end_line=3)
    assert not out.startswith("ERROR"), out
    assert (arena / "m.py").read_text() == ("def total(a, b):\n"
                                            "    if a is None:\n"
                                            "        return 0\n"
                                            "    return a + b\n")


def test_start_line_alone_addresses_one_line(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("notes.txt", new_str="BETA", start_line=2)
    assert not out.startswith("ERROR"), out
    assert (arena / "notes.txt").read_text() == "alpha\nBETA\ngamma\ndelta\n"


def test_a_range_edit_needs_no_old_str(arena, monkeypatch):
    """The whole point: the text that fails to match is never typed."""
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit(file_path="notes.txt", new_str="X", start_line=1, end_line=1)
    assert not out.startswith("ERROR"), out
    assert (arena / "notes.txt").read_text() == "X\nbeta\ngamma\ndelta\n"


def test_a_range_edit_can_delete_the_lines(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("notes.txt", new_str="", start_line=2, end_line=3)
    assert not out.startswith("ERROR"), out
    assert (arena / "notes.txt").read_text() == "alpha\ndelta\n"


def test_the_text_form_still_works_with_the_flag_on(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", "    return a + b", "    return a - b")
    assert not out.startswith("ERROR"), out
    assert bk.last_addressing == "text"


# ------------------------------------------------------------- what it refuses

def test_a_stale_range_is_refused_by_name_with_the_line_count_and_how_to_re_read(arena, monkeypatch):
    """The file was shortened by an earlier edit; the model's remembered range
    now runs past the end. Refused, never clamped."""
    bk = bk_on(arena, monkeypatch)
    before = (arena / "notes.txt").read_text()
    out = bk.t_edit("notes.txt", new_str="X", start_line=3, end_line=9)
    assert out.startswith("ERROR[args]"), out
    assert "notes.txt" in out and "4 lines" in out, out
    assert "start_line" in out and "read(" in out, out
    assert (arena / "notes.txt").read_text() == before
    assert bk.last_edit_record["fail_class"] == "stale_range", bk.last_edit_record
    assert bk.last_addressing == "range"
    assert bk.edit_calls == 1 and bk.edit_failures == 1


def test_a_start_line_past_the_end_is_refused(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("notes.txt", new_str="X", start_line=9, end_line=9)
    assert out.startswith("ERROR[args]") and "4 lines" in out, out
    assert bk.last_edit_record["fail_class"] == "stale_range", bk.last_edit_record


def test_a_backwards_or_zero_range_is_an_argument_error(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    for a, b in ((3, 2), (0, 1), (-1, 2)):
        out = bk.t_edit("notes.txt", new_str="X", start_line=a, end_line=b)
        assert out.startswith("ERROR[args]"), (a, b, out)
    assert (arena / "notes.txt").read_text() == "alpha\nbeta\ngamma\ndelta\n"


def test_a_non_integer_range_is_an_argument_error(arena, monkeypatch):
    out = bk_on(arena, monkeypatch).t_edit("notes.txt", new_str="X", start_line="top")
    assert out.startswith("ERROR[args]"), out


def test_both_addressings_at_once_is_refused(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", old_str="    return a + b", new_str="    return a - b",
                    start_line=4, end_line=4)
    assert out.startswith("ERROR[args]"), out
    assert (arena / "m.py").read_text() == SRC
    assert bk.last_edit_record["fail_class"] == "args", bk.last_edit_record


def test_neither_addressing_is_refused(arena, monkeypatch):
    out = bk_on(arena, monkeypatch).t_edit("m.py", new_str="x")
    assert out.startswith("ERROR[args]"), out


def test_a_range_outside_the_arena_is_blocked(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("/etc/nothing_here.txt", new_str="X", start_line=1)
    assert out.startswith("ERROR[blocked]"), out
    assert bk.last_edit_record["fail_class"] == "outside", bk.last_edit_record


# --------------------------------------------- the laws ride the range form too

def test_the_numeric_only_gate_is_not_bypassed_by_a_range(arena, monkeypatch):
    """Changing HOW you point must not change WHAT is allowed: retuning a
    constant is refused once through a range exactly as through old_str, and
    the identical re-issue overrides it."""
    (arena / "k.py").write_text("LIMIT = 10\n")
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("k.py", new_str="LIMIT = 42", start_line=1, end_line=1)
    assert out.startswith("ERROR[blocked]"), out
    assert (arena / "k.py").read_text() == "LIMIT = 10\n"
    assert bk.last_edit_record["fail_class"] == "numeric_only", bk.last_edit_record
    again = bk.t_edit("k.py", new_str="LIMIT = 42", start_line=1, end_line=1)
    assert not again.startswith("ERROR"), again
    assert (arena / "k.py").read_text() == "LIMIT = 42\n"


def test_the_syntax_guard_rolls_a_range_edit_back(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    out = bk.t_edit("m.py", new_str="def total(a, b:", start_line=1, end_line=1)
    assert out.startswith("ERROR[parse]"), out
    assert (arena / "m.py").read_text() == SRC
    assert bk.last_edit_record["fail_class"] == "parse", bk.last_edit_record


def test_a_range_edit_uncaches_the_file_and_lands_on_the_ledger(arena, monkeypatch):
    bk = bk_on(arena, monkeypatch)
    bk.t_read("notes.txt")
    assert bk.t_read("notes.txt").startswith("[unchanged")
    bk.t_edit("notes.txt", new_str="BETA", start_line=2, end_line=2)
    assert not bk.t_read("notes.txt").startswith("[unchanged")
    assert bk.last_edit_path == str(arena / "notes.txt")
    assert any("notes.txt" in e for e in bk.ledger), bk.ledger


# -------------------------------------------------------------------- the ledger

VERIFY = "sh -c 'exit 1' -- tests/t.py::a"


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


TEXT_SCRIPT = [("edit", {"file_path": "m.py", "old_str": "    return a + b",
                         "new_str": "    return a - b"}),
               ("edit", {"file_path": "m.py", "old_str": "    return nothing_like_this",
                         "new_str": "    return 1"})]

RANGE_SCRIPT = [("edit", {"file_path": "notes.txt", "new_str": "BETA", "start_line": 2, "end_line": 2}),
                ("edit", {"file_path": "notes.txt", "new_str": "X", "start_line": 3, "end_line": 99})]


@pytest.mark.parametrize("flag", ["off", "on"])
def test_every_edit_turn_carries_addressing_under_both_settings(arena, tmp_path, monkeypatch, flag):
    """Measuring is not the lever: the control arm records the field too, or
    the two forms cannot be compared."""
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    if flag == "on":
        monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    Scripted(arena, "the task", TEXT_SCRIPT).run()
    turns = [r for r in records(tmp_path) if r.get("kind") == "turn" and r.get("action") == "edit"]
    assert len(turns) == 2, turns
    assert [t["addressing"] for t in turns] == ["text", "text"], turns
    assert [t["applied"] for t in turns] == [True, False], turns


def test_the_ledger_tells_the_two_forms_apart(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    Scripted(arena, "the task", TEXT_SCRIPT[:1] + RANGE_SCRIPT).run()
    turns = [r for r in records(tmp_path) if r.get("kind") == "turn" and r.get("action") == "edit"]
    assert [t["addressing"] for t in turns] == ["text", "range", "range"], turns
    assert [t["applied"] for t in turns] == [True, True, False], turns
    assert turns[2]["fail_class"] == "stale_range", turns[2]


def test_the_end_record_counts_ranged_edits_under_both_settings(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    Scripted(arena, "the task", RANGE_SCRIPT).run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert {"ranged_edit_calls", "ranged_edits"} <= set(o), sorted(o)
    assert o["edit_calls"] == 2 and o["ranged_edit_calls"] == 2 and o["ranged_edits"] == 1, o


def test_zero_is_a_measurement_the_control_arm_still_carries_the_counts(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    Scripted(arena, "the task", TEXT_SCRIPT[:1]).run()
    o = [r for r in records(tmp_path) if r.get("kind") == "end"][0]["opportunities"]
    assert o["edit_calls"] == 1 and o["ranged_edit_calls"] == 0 and o["ranged_edits"] == 0, o


# ------------------------------------------------------------- the mutation proof

def test_the_stale_range_refusal_is_real(arena, tmp_path, monkeypatch):
    """M-01: a test that cannot fail is worse than a red one. Break the bound
    check in a COPY of the source and prove this suite's stale-range test goes
    red — the mutant applies the edit instead of refusing it."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "beekeeper.py")).read()
    guard = "if b > len(lines):"
    assert src.count(guard) == 1, "the stale-range guard is not where the mutation expects it"
    mutant_path = tmp_path / "beekeeper_mutant.py"
    mutant_path.write_text(src.replace(guard, "if False:"))
    spec = importlib.util.spec_from_file_location("beekeeper_mutant", mutant_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("BEEKEEPER_RANGED_EDIT", "on")
    bk = mod.Beekeeper(str(arena), "t")
    out = bk.t_edit("notes.txt", new_str="X", start_line=3, end_line=9)
    assert not out.startswith("ERROR"), ("without the guard the stale range is not refused; "
                                         "with it, it is — the refusal is the guard's doing")
    assert (arena / "notes.txt").read_text() != "alpha\nbeta\ngamma\ndelta\n"
