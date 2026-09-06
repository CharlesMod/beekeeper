"""H-33 and H-15 — alternation, no-progress, and the wander law: the stall
law extended from period 1 to periods 2 and 3.

The stall law (test_stall.py) compares a result only against the IMMEDIATELY
previous one, so it sees a period-1 loop and nothing else. Pool v2's remaining
stalls were period 2: two actions taken in turns, each returning its own
unchanged result, neither ever equal to the result before it — the detector
never fired and the run wandered to the cap. The same hole lets a detour of
one or two unrelated actions between two refusals reset `stall_refusals` to
zero forever.

Laws (BEEKEEPER_ALT=on; off by default, the harness byte-identical when off):
  - ALTERNATION: an A-B-A-B (or A-B-C-A-B-C) pattern of refused or
    unchanged-result actions is ONE refusal streak, not a fresh start each
    time. The trigger is the period-1 law's own threshold generalized —
    every member of the cycle has returned its identical result three
    times — and it exhausts the cycle's actions, so the pair ends the run
    as stalled instead of wandering to the cap;
  - NO PROGRESS: N consecutive edits (default 3, BEEKEEPER_ALT_EDITS) with
    the verify outcome unchanged — the same failing count and the same
    failing test set — withhold `edit` for one turn and say so in one line;
    a recurrence counts toward the stall streak;
  - THE WANDER: a detour of one or two unrelated actions between two
    refusals does not reset the streak; a third unrelated action does (it
    is a new line of work, not a wander);
  - the successful-edit exemption of the stall law stands untouched:
    progress is never punished, and it clears everything;
  - every trigger is a spend-ledger record {"kind":"alt","turn":…,
    "pattern":"aba"|"no_progress"|"wander",…}, and the end record carries
    the opportunity counts M-04 needs: alternations, no_progress_events,
    wanders.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402

A = ("bash", {"command": "echo a >> a.txt; echo same-a"})   # identical result, side effect counts runs
B = ("bash", {"command": "echo b >> b.txt; echo same-b"})   # identical result, different from A's
C = ("bash", {"command": "echo c >> c.txt; echo same-c"})
GROW = ("bash", {"command": "echo g >> g.txt; cat g.txt"})  # a real detour: the result differs every time
VERIFY = ("bash", {"command": "sh v.sh"})

WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
EDITS = [("edit", {"file_path": "f.py", "old_str": WORDS[i], "new_str": WORDS[i] + "_x"})
         for i in range(len(WORDS))]                        # distinct, successful, non-numeric
FIX = ("edit", {"file_path": "f.py", "old_str": "golf", "new_str": "fixed"})


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify_cmd=None, **kw):
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


V_SH = """if grep -q fixed f.py 2>/dev/null; then
  echo "FAILED tests/test_x.py::test_b"; echo "1 failed, 1 passed"; exit 1
fi
echo "FAILED tests/test_x.py::test_a"
echo "FAILED tests/test_x.py::test_b"
echo "2 failed"
exit 1
"""


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    for k in ("BEEKEEPER_ALT", "BEEKEEPER_ALT_EDITS", "BEEKEEPER_SPEND_DIR"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text("\n".join(WORDS) + "\n")
    (a / "v.sh").write_text(V_SH)
    return a


def runs(arena, name="a.txt"):
    p = arena / name
    return len(p.read_text().splitlines()) if p.exists() else 0


def alt_records(tmp_path, pattern=None):
    recs = [json.loads(l) for f in (tmp_path / "spend").glob("*.jsonl")
            for l in open(f) if l.strip()]
    return [r for r in recs if r.get("kind") == "alt"
            and (pattern is None or r.get("pattern") == pattern)]


def end_record(tmp_path):
    recs = [json.loads(l) for f in (tmp_path / "spend").glob("*.jsonl")
            for l in open(f) if l.strip()]
    return [r for r in recs if r.get("kind") == "end"][0]


# ---------- law 1: alternation ----------

def test_off_by_default_an_alternating_pair_runs_to_the_cap(arena, capsys):
    """The hole, stated as a control: A-B-A-B never trips the period-1 law —
    neither result is ever equal to the one before it."""
    rc = Scripted(arena, [A, B] * 12).run()
    out = capsys.readouterr().out
    assert runs(arena) == 12 and runs(arena, "b.txt") == 12, "every call ran: nothing was refused"
    assert "refused" not in out and "stalled" not in out
    assert rc != 3


def test_an_alternating_pair_ends_the_run_as_stalled(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, [A, B] * 12)
    rc = bk.run()
    out = capsys.readouterr().out
    assert rc == 3, f"exit 3 names the stall; got {rc}"
    assert "stalled" in out, out
    assert runs(arena) == 3, f"A ran {runs(arena)}x — the cycle is exhausted at three identical results"
    assert bk.turn <= 12, f"the pair ended the run, not the turn cap (turn {bk.turn})"
    assert bk.alternations >= 1


def test_a_period_three_cycle_counts_too(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, [A, B, C] * 12)
    rc = bk.run()
    assert rc == 3, capsys.readouterr().out
    assert bk.alternations >= 1 and runs(arena) == 3


def test_a_changing_result_never_forms_a_cycle(arena, monkeypatch, capsys):
    """GROW's result differs every time: alternating it with A is a search,
    not a loop, and only A's own period-1 exhaustion may fire."""
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, [GROW, B] * 6)
    rc = bk.run()
    assert bk.alternations == 0, "no member of the cycle repeats a result"
    assert rc != 3 and runs(arena, "g.txt") == 6


# ---------- law 3: the wander ----------

def test_one_detour_between_refusals_does_not_reset_the_streak(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, [A, A, A] + [A, GROW] * 6)
    rc = bk.run()
    out = capsys.readouterr().out
    assert rc == 3 and "stalled" in out, out
    assert bk.wanders >= 1, "a detour that kept a streak alive is an opportunity, and it is counted"
    assert runs(arena) == 3


def test_off_by_default_a_detour_resets_the_streak(arena, capsys):
    rc = Scripted(arena, [A, A, A] + [A, GROW] * 6).run()
    out = capsys.readouterr().out
    assert "refused" in out, "the period-1 law still exhausts A"
    assert "stalled" not in out and rc != 3, "with the flag off, every detour resets the streak"


def test_three_unrelated_actions_are_a_new_line_of_work(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, [A, A, A] + [A, GROW, GROW, GROW] * 4)
    rc = bk.run()
    out = capsys.readouterr().out
    assert rc != 3 and "stalled" not in out, "a third detour resets the streak"
    assert bk.stall_refusals <= 1
    assert bk.wanders >= 2, "the first two detours of each round kept the streak alive and are counted"


# ---------- law 2: no progress ----------

def test_three_flat_edits_withhold_edit_for_one_turn_and_say_so(arena, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, EDITS[:3] + [VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert bk.no_progress_events == 1 and bk.stall_refusals == 0, "the first trigger withholds, it does not stall"
    names = [t["function"]["name"] for t in bk.tools()]
    assert "edit" not in names and {"read", "bash", "done"} <= set(names), names
    assert "edit" in [t["function"]["name"] for t in bk.tools()], "one turn only"
    line = [m["content"] for m in bk.messages
            if m.get("role") == "user" and str(m.get("content", "")).startswith("[no progress")]
    assert len(line) == 1 and line[0].count("\n") == 0, line
    recs = alt_records(tmp_path, "no_progress")
    assert len(recs) == 1 and recs[0]["edits"] == 3 and recs[0]["failing"] == 2, recs
    assert recs[0]["turn"] == 4, recs


def test_the_verify_moving_is_never_punished(arena, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [EDITS[0], EDITS[1], FIX, VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert bk.no_progress_events == 0, "the failing set changed: three edits made progress"
    assert "edit" in [t["function"]["name"] for t in bk.tools()]
    assert alt_records(tmp_path) == []


def test_a_recurrence_counts_toward_the_stall_streak(arena, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, EDITS[:3] + [VERIFY] + EDITS[3:6] + [VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert bk.no_progress_events == 2
    assert bk.stall_refusals == 1, "the first trigger withholds; the recurrence joins the streak"
    recs = alt_records(tmp_path, "no_progress")
    assert len(recs) == 2 and recs[0]["streak"] == 0 and recs[1]["streak"] == 1, recs


def test_edits_that_never_move_the_verify_end_the_run_as_stalled(arena, monkeypatch, capsys):
    """The streak the recurrences feed is the stall law's own: eighteen edits
    and six verifies with the failing set exactly where it started end the
    run at exit 3, with the turn finished and recorded, not mid-call."""
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    ping = [("edit", {"file_path": "f.py",
                      "old_str": "alpha" if i % 2 == 0 else "alpha_x",
                      "new_str": "alpha_x" if i % 2 == 0 else "alpha"}) for i in range(18)]
    script = sum(([ping[i], ping[i + 1], ping[i + 2], VERIFY] for i in range(0, 18, 3)), [])
    bk = Scripted(arena, script, verify_cmd="sh v.sh")
    rc = bk.run()
    out = capsys.readouterr().out
    assert rc == 3 and "stalled" in out, out
    assert bk.no_progress_events == 6 and bk.stall_refusals == 5
    roles = [m["role"] for m in bk.messages]
    assert roles[-2:] == ["tool", "user"], "the turn was finished and recorded, not dropped mid-call"
    assert str(bk.messages[-1]["content"]).startswith("[no progress"), bk.messages[-1]


def test_a_verify_that_moves_restores_the_exemption(arena, monkeypatch):
    """Law 5, measured rather than assumed: while the harness has just shown
    that the edits moved nothing, an edit no longer clears the streak (the
    recurrence clause would be dead letter otherwise) — and the moment a
    verify moves, the very next edit clears it again. Never punished either
    way: no refusal, no exhaustion, no withhold beyond the one turn."""
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    undo = ("edit", {"file_path": "f.py", "old_str": "alpha_x", "new_str": "alpha"})
    bk = Scripted(arena, EDITS[:3] + [VERIFY] + EDITS[3:6] + [VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert bk.stall_refusals == 1 and bk.alt_flat is True

    bk2 = Scripted(arena, EDITS[:3] + [VERIFY] + EDITS[3:6] + [VERIFY, FIX, VERIFY, undo],
                   verify_cmd="sh v.sh")
    bk2.run()
    assert bk2.no_progress_events == 2, "the same two triggers"
    assert bk2.alt_flat is False and bk2.stall_refusals == 0, "the verify moved; the edit cleared the streak"
    assert "edit" in [t["function"]["name"] for t in bk2.tools()]


def test_the_edit_count_is_a_flag(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_ALT_EDITS", "2")
    bk = Scripted(arena, EDITS[:2] + [VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert bk.alt_edits == 2 and bk.no_progress_events == 1


def test_off_by_default_flat_edits_are_not_withheld(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, EDITS[:3] + [VERIFY], verify_cmd="sh v.sh")
    bk.run()
    assert "edit" in [t["function"]["name"] for t in bk.tools()]
    assert alt_records(tmp_path) == []


# ---------- law 5: progress is never punished ----------

def test_the_successful_edit_exemption_stands(arena, monkeypatch, capsys):
    """test_stall.py's law, under the flag: five distinct successful edits
    share a path and a confirmation text and none is a repeat; and an edit
    after a refusal clears the exhaustion and the streak."""
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    bk = Scripted(arena, EDITS[:5])
    bk.run()
    out = capsys.readouterr().out
    assert "refused" not in out and "identical to your previous" not in out, out
    assert bk.alternations == 0 and bk.no_progress_events == 0 and bk.wanders == 0

    bk2 = Scripted(arena, [A, A, A, A, EDITS[5], A])
    bk2.run()
    assert bk2.stall_refusals == 0, "a successful edit clears the streak"
    assert runs(arena) == 4, "and clears the exhaustion: the probe is new information"


# ---------- laws 4: the ledger and the settings line ----------

def test_the_end_record_carries_the_opportunity_counts(arena, monkeypatch, tmp_path):
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [A, B] * 12)
    bk.run()
    end = end_record(tmp_path)
    assert end["alt"] == "on"
    for k in ("alternations", "no_progress_events", "wanders"):
        assert k in end, (k, end)
    assert end["alternations"] == bk.alternations >= 1
    recs = alt_records(tmp_path, "aba")
    assert recs and recs[0]["period"] == 2 and recs[0]["streak"] >= 1 and "turn" in recs[0], recs
    assert len(recs[0]["sigs"]) == 2


def test_zero_opportunities_is_still_reported(arena, monkeypatch, tmp_path):
    """M-04: a lever with no opportunity reports zero, it does not vanish."""
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    Scripted(arena, [GROW, GROW]).run()
    end = end_record(tmp_path)
    assert end["alt"] == "off"
    assert (end["alternations"], end["no_progress_events"], end["wanders"]) == (0, 0, 0)


def test_an_alternation_ends_in_a_restart_not_sixty_turns(arena, monkeypatch):
    """H-33's validate column, with H-12 on the same tree: the pair that used
    to wander to the cap now ends attempt 1 stalled, and the clock buys a
    fresh context instead of fifty more turns of the same two calls."""
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_RESTART", "on")
    made = []

    def make(attempt, notes, prev):
        bk = Scripted(arena, [A, B] * 12 if attempt == 1 else [B],
                      start_verify=(attempt == 1),
                      net_baseline=(prev.net_baseline if prev is not None else None))
        made.append(bk)
        return bk

    rc, attempts = beekeeper.run_attempts(make, max_seconds=600)
    assert attempts[0]["rc"] == 3 and attempts[0]["reason"] == "stalled", attempts
    assert attempts[0]["turns"] <= 12 and len(attempts) == 2, attempts
    assert len(made) == 2


def test_alt_is_named_in_the_settings_line(arena, monkeypatch, capsys):
    Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "alt=off(default)" in line and "alt_edits=3" in line, line
    monkeypatch.setenv("BEEKEEPER_ALT", "on")
    monkeypatch.setenv("BEEKEEPER_ALT_EDITS", "5")
    Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "alt=on(env)" in line and "alt_edits=5" in line, line
