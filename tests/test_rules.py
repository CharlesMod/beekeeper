"""H-07, the lean prompt: delete the rules the guards already enforce.

M-09's direction, applied to the system prompt itself. Seven numbered rules
sit in front of a model that does not read them — "ONE READ EACH" was
violated sixty times in one run while it was in view — and every one of them
except two is already a guard in this file: the read cache and the collapse
enforce rule 4, the numeric-literal gate enforces rule 2, the nudge counter
enforces rule 3, the start verify and the phase gate enforce rule 1, the
done gate enforces the verify half of rule 5, and every refusal names its own
rule, which is rule 7. What no guard enforces is the task's own self-check
and submission step, and the exit-code law that decides the run. Laws:

  - `BEEKEEPER_RULES` selects the variant: default `full` is today's prompt,
    BYTE-IDENTICAL, so an unflagged run is unchanged; `lean` is the variant;
  - the lean prompt contains none of the deleted sentences and all of the
    kept ones: it names the tools, the verify command, the self-check and
    submission step, and the exit-code law;
  - the arena anchor keeps the FACT (where bash runs) and drops the RULE
    ("never leave it") — the scope fence enforces it, and still does under
    lean, which is why the sentence can go;
  - the settings line names the variant and its source, like every other
    lever.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import SYSTEM, Beekeeper  # noqa: E402

VERIFY = "python3 -m pytest -q tests/t.py"

# The sentences a guard already enforces. Each is quoted from today's prompt.
DELETED = [
    "DIAGNOSE FIRST",
    "OBSERVE the failure before reading much code",
    "FIX CAUSES, NEVER FIT NUMBERS",
    "Never retune a constant or threshold",
    "ACT, DON'T NARRATE",
    "Prefer a tool call over prose",
    "ONE READ EACH",
    "Re-reading an unchanged file wastes the clock",
    "After edits, re-run the tests",
    "WHEN THE HARNESS REFUSES",
    "it is never noise",
]


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.delenv("BEEKEEPER_RULES", raising=False)
    monkeypatch.delenv("BEEKEEPER_PHASE", raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("x = 1\n")
    return a


def build(arena, **kw):
    kw.setdefault("start_verify", False)
    return Beekeeper(str(arena), "the task text", verify_cmd=VERIFY, **kw)


def system_of(bk):
    return bk.messages[0]["content"]


def anchor_of(bk):
    return bk.messages[1]["content"]


def test_the_default_is_todays_prompt_byte_identical(arena):
    bk = build(arena)
    assert bk.rules_policy == "full"
    assert system_of(bk) == SYSTEM
    assert "Use relative paths; never leave it." in anchor_of(bk)
    assert anchor_of(bk).endswith("the task text")


def test_lean_deletes_every_rule_a_guard_enforces(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    text = system_of(build(arena))
    for sentence in DELETED:
        assert sentence not in text, sentence
    # the seven-rule scaffolding goes with them
    assert "DISCIPLINE" not in text
    assert "\n1." not in text


def test_lean_keeps_what_no_guard_enforces(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    text = system_of(build(arena))
    for tool in ("read", "edit", "write", "bash", "done"):
        assert tool in text, tool
    assert VERIFY in text                      # the verify command, named
    assert "self-check" in text                 # no guard runs the task's own check
    assert "submission" in text                 # nor its submission step
    assert "exits 0" in text and "Exit codes decide" in text   # the exit-code law


def test_lean_drops_the_anchor_rule_and_keeps_the_anchor_fact(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    anchor = anchor_of(build(arena))
    assert str(arena.resolve()) in anchor          # the fact: where bash runs
    assert "your bash commands run there" in anchor
    assert "never leave it" not in anchor          # the rule: the scope fence has it
    assert anchor.endswith("the task text")


def test_the_scope_fence_still_enforces_the_deleted_sentence(arena, monkeypatch):
    """The deletion is only safe because the guard is real: prove it under lean."""
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    bk = build(arena)
    assert "outside the arena" in bk.t_read("/etc/hosts")


def test_the_settings_line_names_the_variant(arena, monkeypatch, capsys):
    build(arena)
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l]
    assert line and "rules=full(default)" in line[0], line
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    build(arena)
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l]
    assert line and "rules=lean(env)" in line[0], line


def test_lean_is_a_fraction_of_the_prompt(arena, monkeypatch):
    """chars/2.5 is the house estimate; the deletion must be worth measuring."""
    full = len(SYSTEM) / 2.5
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    lean = len(system_of(build(arena))) / 2.5
    assert lean < full / 2, (lean, full)


def test_an_unknown_variant_falls_back_to_full(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "sparse")
    bk = build(arena)
    assert bk.rules_policy == "full"
    assert system_of(bk) == SYSTEM


def test_the_end_record_names_the_variant_and_its_measured_size(arena, monkeypatch, tmp_path):
    """M-01: the arm's own instrument is recorded, not inferred from a log line."""
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = build(arena)
    bk._end("done", 0)
    import glob, json
    recs = [json.loads(l) for f in glob.glob(str(tmp_path / "spend" / "*.jsonl"))
            for l in open(f)]
    end = [r for r in recs if r.get("kind") == "end"][-1]
    assert end["rules_policy"] == "lean"
    assert 0 < end["prompt_tokens_est"] < len(SYSTEM) / 2.5


def tool_line(body):
    return [l for l in body["messages"][0]["content"].splitlines()
            if l.startswith("Tools:")][0]


def test_lean_names_only_the_tools_the_turn_offers(arena, monkeypatch):
    """The prompt is rendered from the SAME array the request carries, so the
    phase gate and the withhold law can never leave the two disagreeing."""
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")
    bk = build(arena)
    bk._phase_gate(1)                      # turn one: edit and write are out
    body = bk._request_body()
    offered = [t["function"]["name"] for t in body["tools"]]
    assert "edit" not in offered and "write" not in offered
    line = tool_line(body)
    assert "edit" not in line and "write" not in line
    assert [n.strip() for n in line[len("Tools:"):].strip().rstrip(".").split(",")] == offered


def test_the_render_is_per_turn_not_sticky(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_RULES", "lean")
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")
    bk = build(arena)
    bk._phase_gate(1)
    bk._request_body()
    bk.phase_verified = True               # the verify has been observed
    bk._phase_gate(2)
    body = bk._request_body()
    offered = [t["function"]["name"] for t in body["tools"]]
    assert "edit" in offered and "edit" in tool_line(body)
    assert [n.strip() for n in tool_line(body)[len("Tools:"):].strip().rstrip(".").split(",")] == offered


def test_full_is_untouched_by_the_per_turn_render(arena, monkeypatch):
    """The control: the schema withholds, the prompt does not move a byte."""
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")
    bk = build(arena)                      # BEEKEEPER_RULES unset -> full
    bk._phase_gate(1)
    body = bk._request_body()
    assert "edit" not in [t["function"]["name"] for t in body["tools"]]
    assert body["messages"][0]["content"] == SYSTEM
