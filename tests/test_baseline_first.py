"""The baseline is a record, always — H-09's law made unfalsifiable.

Measured on pool v2 (chain 3, 2026-09-06): seven runs under BEEKEEPER_NET=on
or =bg EDITED with no `{"kind":"net"}` baseline record anywhere in their spend
ledger (alt x2, autoverify x2, create x1, netbg x2). One of them reads: turn 1
read, turn 2 edit, turn 3 read, turn 4 edit, and not one net record. The net's
gate cannot fire without a baseline, so those runs' net opportunity was
silently zero — and silence reads exactly like "the lever did nothing".

`_net_baseline` returns without a word in four places: the verify names no
test ids (no net command derivable), the baseline was carried in from a
restart, the background baseline never landed because the run ended first,
and — worse — a net command that measured NOTHING (exit 4, "no tests ran")
is read as "zero failing", which makes every red sibling at `done` a
regression (M-02: a red exit is not evidence that tests ran).

The law:
  - under any policy but `off`, one `{"kind":"net","stage":"baseline"}`
    record per run, and it carries the turn it was taken on: never later
    than the turn of the first APPLIED edit;
  - a baseline that cannot be measured is `failing: null` with a `reason`
    — never silence, and never `0`;
  - an unmeasured baseline is not a licence to call siblings regressions:
    the gate says so instead of judging on a baseline it does not have;
  - under `bg`, a run that ends before the thread lands records what it has
    (`pending: true`), so the analyst reads "not landed", never
    "not attempted";
  - a restart that carries its predecessor's baseline names it in its own
    ledger (`carried: true`).

tests/test_net.py and tests/test_net_bg.py are the pinned laws and are never
edited; this file only adds.
"""
import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

# the `::` form measures; the whole-file (net) form is the one a test can make
# slow (NET_SLEEP) or unmeasurable (NET_BROKEN: pytest's exit 4, nothing ran)
RUNNER = textwrap.dedent('''
    import os, sys, time
    KNOWN = {"tests/test_a.py": ["test_x", "test_y", "test_z"]}
    want, whole = [], False
    for a in sys.argv[1:]:
        if "::" in a:
            f, n = a.split("::"); want.append((f, n))
        elif a in KNOWN:
            whole = True
            want += [(a, n) for n in KNOWN[a]]
    if whole:
        time.sleep(float(os.environ.get("NET_SLEEP") or 0))
        if os.path.exists("net_broken"):        # the FIRST net run only: the baseline's
            os.remove("net_broken")             # own run is the one that cannot collect
            print("ERROR: file or directory not found: tests/test_a.py")
            print("no tests ran in 0.01s")
            sys.exit(4)
    failed = [f"{f}::{n}" for f, n in want if not os.path.exists(f"pass_{n}")]
    for t in failed: print(f"FAILED {t} - boom")
    npass = len(want) - len(failed)
    print(f"{len(failed)} failed, {npass} passed" if failed else f"{npass} passed")
    sys.exit(1 if failed else 0)
''')
VERIFY = "python3 runtests.py tests/test_a.py::test_x"
NO_IDS = "sh -c 'test -f pass_test_x'"          # a real gate that names no tests
X, Y = "tests/test_a.py::test_x", "tests/test_a.py::test_y"

READ = ("read", {"file_path": "mod.py"})
EDIT = ("edit", {"file_path": "mod.py", "old_str": "one", "new_str": "two"})
EDIT2 = ("edit", {"file_path": "mod.py", "old_str": "two", "new_str": "three"})
FIX_X = ("write", {"file_path": "pass_test_x", "content": "fixed\n"})
SLOW = ("bash", {"command": "sleep 0.6"})
DONE = ("done", {"summary": "done"})


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify=VERIFY, **kw):
        super().__init__(str(arena), "the task", verify_cmd=verify, **kw)
        self.script = list(script)

    def request(self):
        self._request_body()
        if not self.script:
            return {"message": {"content": ""}, "finish_reason": "stop"}
        name, args = self.script.pop(0)
        return {"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
            "finish_reason": "tool_calls"}


def _arena(tmp_path, name="arena"):
    a = tmp_path / name
    a.mkdir()
    (a / "runtests.py").write_text(RUNNER)
    (a / "mod.py").write_text("value = 'one'\n")
    (a / "tests").mkdir()
    (a / "tests" / "test_a.py").write_text("# tests\n")
    (a / "pass_test_z").write_text("")          # z passes at baseline; x and y fail
    return a


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    for k in ("BEEKEEPER_NET", "BEEKEEPER_ALT", "BEEKEEPER_PHASE", "BEEKEEPER_PHASE_K",
              "BEEKEEPER_AUTOVERIFY", "BEEKEEPER_BOARD", "BEEKEEPER_RESTART",
              "NET_SLEEP"):
        monkeypatch.delenv(k, raising=False)
    return _arena(tmp_path)


def _records(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


def _baseline(tmp_path):
    recs = [r for r in _records(tmp_path)
            if r.get("kind") == "net" and r.get("stage") == "baseline"]
    assert len(recs) == 1, f"exactly one baseline record per run, got {recs}"
    return recs[0]


def _end(tmp_path):
    return [r for r in _records(tmp_path) if r.get("kind") == "end"][-1]


def test_the_measured_baseline_still_reads_as_it_did(arena, tmp_path):
    """The control: nothing about a baseline that measures cleanly changes."""
    bk = Scripted(arena, [READ, EDIT, DONE])
    bk.run()
    b = _baseline(tmp_path)
    assert b["failing"] == 2 and sorted(b["names"]) == [X, Y], b
    assert bk.net_baseline == {X, Y}


def test_the_baseline_record_is_never_later_than_the_first_edit(arena, tmp_path):
    """The seven-run shape under alternation: read, edit, read, edit."""
    os.environ["BEEKEEPER_ALT"] = "on"
    try:
        Scripted(arena, [READ, EDIT, READ, EDIT2]).run()
    finally:
        del os.environ["BEEKEEPER_ALT"]
    b, e = _baseline(tmp_path), _end(tmp_path)
    assert e["first_edit_turn"] == 2, e
    assert b["turn"] <= e["first_edit_turn"], (b, e)


def test_the_phase_gates_forced_first_edit_gets_its_baseline_first(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_PHASE", "on")
    monkeypatch.setenv("BEEKEEPER_PHASE_K", "2")
    Scripted(arena, [READ, EDIT, DONE]).run()
    b, e = _baseline(tmp_path), _end(tmp_path)
    assert e["first_edit_turn"] is not None, e
    assert b["turn"] <= e["first_edit_turn"], (b, e)


def test_a_verify_that_names_no_tests_records_the_reason_not_silence(arena, tmp_path):
    """No `::` in the verify means no net command — which is a FACT about
    the run, and a fact belongs in the ledger."""
    bk = Scripted(arena, [READ, EDIT, FIX_X, DONE], verify=NO_IDS)
    assert bk.net_cmd is None
    bk.run()
    b = _baseline(tmp_path)
    assert b["failing"] is None and b.get("reason"), b
    assert _end(tmp_path)["first_edit_turn"] == 2


def test_a_baseline_that_measured_nothing_is_null_not_zero(arena, tmp_path):
    """M-02: exit 4 with 'no tests ran' proves nothing about the tree. An
    empty failing set is a measurement of zero; this is not one."""
    (arena / "net_broken").write_text("")
    bk = Scripted(arena, [READ, EDIT], verify=VERIFY)
    bk.run()
    b = _baseline(tmp_path)
    assert b["failing"] is None and b.get("reason"), b
    assert bk.net_baseline is None, "an unmeasured baseline is not the empty set"


def test_an_unmeasured_baseline_does_not_make_every_sibling_a_regression(arena, tmp_path, capsys):
    """The gate judges on a baseline or it does not judge. With none, `done`
    is not refused for a 'regression' that was never observed to pass."""
    (arena / "net_broken").write_text("")     # the baseline cannot collect; the gate's own run can
    rc = Scripted(arena, [FIX_X, DONE]).run()
    out = capsys.readouterr().out
    assert "regression" not in out, out       # test_y was never observed to pass
    assert rc == 0, out
    d = [r for r in _records(tmp_path) if r.get("kind") == "net" and r.get("stage") == "done"]
    assert d and d[0]["baseline"] is None, d


def test_bg_that_ends_before_the_baseline_lands_records_it_pending(arena, tmp_path, monkeypatch):
    """H-09b's honest end: 'not landed' is a reading; nothing is not."""
    monkeypatch.setenv("BEEKEEPER_NET", "bg")
    monkeypatch.setenv("NET_SLEEP", "10")
    bk = Scripted(arena, [SLOW, SLOW])
    rc = bk.run(max_seconds=0.3)
    assert rc != 0
    b = _baseline(tmp_path)
    assert b["pending"] is True and b["failing"] is None, b


def test_a_carried_baseline_names_itself_in_the_new_ledger(arena, tmp_path):
    """A restart re-uses the baseline measured before the FIRST edit of the
    first attempt (beekeeper.main). Its own ledger must say so."""
    bk = Scripted(arena, [], net_baseline={X, Y}, start_verify=False)
    assert bk.net_baseline == {X, Y}
    b = _baseline(tmp_path)
    assert b["carried"] is True and b["failing"] == 2, b


def test_net_off_owes_no_baseline_record(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_NET", "off")
    Scripted(arena, [READ, EDIT, DONE]).run()
    assert not [r for r in _records(tmp_path)
                if r.get("kind") == "net"], "off is declared in the end record, not paid for"


def test_an_edit_that_never_applied_is_not_an_edit_that_owes_a_baseline(arena, tmp_path):
    """The false positive under the same law. A turn's `action` is stamped
    before the tool runs, so a refused or unapplied edit reads as "edit" in
    the ledger while `t_edit` never reached the baseline — correctly, since
    nothing was written. The record says which of the two it was."""
    MISS = ("edit", {"file_path": "mod.py", "old_str": "nope", "new_str": "x"})
    Scripted(arena, [READ, MISS, READ, MISS]).run()
    turns = [r for r in _records(tmp_path) if r.get("action") == "edit"]
    assert turns and all(r["applied"] is False for r in turns), turns
    e = _end(tmp_path)
    assert e["first_edit_turn"] is None, e
    b = _baseline(tmp_path)
    assert b["failing"] is None and b["reason"] == "not_reached", b
