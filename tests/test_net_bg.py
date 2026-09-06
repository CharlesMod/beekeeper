"""H-09b, the baseline in the background: the regression net's baseline runs
on a start-of-run snapshot of the tree, in a thread, while the model reads —
so a run that edits pays nothing on the clock for it.

Measured on pool v2: the net gate fired in 0 of 28 runs, and 10 of those runs
still paid a full-file docker verify (20-90 s of a 480 s cap) for a baseline
nobody used. The law it must not weaken is H-09's: the baseline PRECEDES the
first edit. So the snapshot is copied before the model's first turn, the
command runs there, and an edit that arrives while the thread is still
running WAITS for it — and the wait is a ledger record, never a silent cost.

Laws (`BEEKEEPER_NET=bg`; `on` keeps today's synchronous baseline, `off` is
unchanged, and every value is declared in the settings line):
  - the baseline is measured on a copy of the tree taken before turn 1, and
    the command runs in that copy;
  - a run that edits before the thread finishes waits for it, and the wait
    is a `{"kind":"net","stage":"wait"}` record;
  - a run whose baseline finished first pays nothing and records no wait;
  - the snapshot lives outside the arena (the tamper monitor walks the
    arena) and is removed at the end of the run;
  - the gate itself is untouched: a regression still refuses `done`
    outright, a pre-existing sibling still refuses it once by name.

tests/test_net.py is the pinned law and is never edited; this file only adds.
"""
import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

# the net form (whole files, no `::`) is the slow one — NET_SLEEP holds the
# thread open so a test can decide whether an edit had to wait for it
RUNNER = textwrap.dedent('''
    import os, sys, time
    KNOWN = {"tests/test_a.py": ["test_x", "test_y", "test_z"]}
    want, slow = [], False
    for a in sys.argv[1:]:
        if "::" in a:
            f, n = a.split("::"); want.append((f, n))
        elif a in KNOWN:
            slow = True
            want += [(a, n) for n in KNOWN[a]]
    if slow:
        time.sleep(float(os.environ.get("NET_SLEEP") or 0))
    failed = [f"{f}::{n}" for f, n in want if not os.path.exists(f"pass_{n}")]
    for t in failed: print(f"FAILED {t} - boom")
    npass = len(want) - len(failed)
    print(f"{len(failed)} failed, {npass} passed" if failed else f"{npass} passed")
    sys.exit(1 if failed else 0)
''')
VERIFY = "python3 runtests.py tests/test_a.py::test_x"
X, Y, Z = ("tests/test_a.py::test_x", "tests/test_a.py::test_y", "tests/test_a.py::test_z")

FIX_X = ("write", {"file_path": "pass_test_x", "content": "fixed\n"})
PASS_Y = ("bash", {"command": "touch pass_test_y"})
BREAK_Z = ("bash", {"command": "rm -f pass_test_z"})
SLEEP = ("bash", {"command": "sleep 0.5"})
DONE = ("done", {"summary": "done"})


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify=VERIFY):
        super().__init__(str(arena), "the task", verify_cmd=verify)
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
    (a / "tests").mkdir()
    (a / "tests" / "test_a.py").write_text("# tests\n")
    (a / "pass_test_z").write_text("")          # z passes at baseline; x and y fail
    return a


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.setenv("BEEKEEPER_NET", "bg")
    monkeypatch.delenv("NET_SLEEP", raising=False)
    for k in ("BEEKEEPER_AUTOVERIFY", "BEEKEEPER_BOARD", "BEEKEEPER_SPEND_DIR", "BEEKEEPER_RESTART"):
        monkeypatch.delenv(k, raising=False)
    return _arena(tmp_path)


def _records(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


def test_bg_is_declared_and_measures_the_same_baseline(arena, capsys):
    bk = Scripted(arena, [FIX_X, DONE])
    assert bk.net_snapshot and bk.net_thread, "the copy and the thread start before turn 1"
    rc = bk.run()
    out = capsys.readouterr().out
    assert "net=bg(env)" in [l for l in out.splitlines() if "settings:" in l][0]
    assert bk.net_baseline == {X, Y}, bk.net_baseline
    assert rc == 0 or "done refused" in out          # the gate still runs; see the gate tests below


def test_the_baseline_is_the_start_of_run_tree_and_runs_in_the_snapshot(arena, tmp_path, monkeypatch):
    """The discriminator: the model makes test_y pass with a bash BEFORE its
    first edit. Synchronous, the baseline is measured at that first edit and
    reads y as passing; in the background it is the tree as the run started —
    and because the command runs in the copy, the live `touch` cannot reach
    it even though the thread is still running when it lands."""
    monkeypatch.setenv("NET_SLEEP", "1.0")
    bk = Scripted(arena, [PASS_Y, FIX_X, DONE])
    bk.run()
    assert bk.net_baseline == {X, Y}, bk.net_baseline

    monkeypatch.setenv("BEEKEEPER_NET", "on")
    sync = Scripted(_arena(tmp_path, "sync"), [PASS_Y, FIX_X, DONE])
    sync.run()
    assert sync.net_baseline == {X}, "synchronous: the baseline is the tree at the first edit"


def test_an_edit_before_the_thread_finishes_waits_and_the_wait_is_a_record(arena, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NET_SLEEP", "1.0")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [FIX_X, DONE])
    bk.run()
    waits = [r for r in _records(tmp_path) if r.get("kind") == "net" and r.get("stage") == "wait"]
    assert len(waits) == 1, waits
    assert waits[0]["waited"] > 0.3, waits
    assert bk.net_baseline == {X, Y}
    assert "waited" in capsys.readouterr().out


def test_a_baseline_that_finished_first_costs_the_run_nothing(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [SLEEP, FIX_X, DONE])
    bk.run()
    recs = _records(tmp_path)
    assert not [r for r in recs if r.get("kind") == "net" and r.get("stage") == "wait"], recs
    assert bk.net_baseline == {X, Y}
    base = [r for r in recs if r.get("kind") == "net" and r.get("stage") == "baseline"]
    assert base and base[0]["bg"] is True, base


def test_the_snapshot_is_outside_the_arena_and_removed_at_the_end(arena):
    bk = Scripted(arena, [FIX_X, DONE])
    snap = bk.net_snapshot
    assert snap and os.path.isdir(snap), snap
    assert not os.path.realpath(snap).startswith(str(arena)), snap
    bk.run()
    assert not os.path.exists(snap), "the copy does not outlive the run"
    assert bk._changed_files() == {"pass_test_x"}, "the snapshot never touched the tree"


def test_the_gate_is_untouched_under_bg(arena, tmp_path, capsys):
    """The control: green before this lever and green after it. A regression
    still refuses done outright; a pre-existing sibling still refuses it once
    by name and a re-issued done accepts it."""
    rc = Scripted(arena, [FIX_X, BREAK_Z, DONE, DONE]).run()
    out = capsys.readouterr().out
    assert rc != 0 and "DONE (verified)" not in out
    assert "regression" in out and "test_z" in out, out

    rc2 = Scripted(_arena(tmp_path, "second"), [FIX_X, DONE, DONE]).run()
    out2 = capsys.readouterr().out
    assert "done refused" in out2 and "test_y" in out2, out2
    assert rc2 == 0 and out2.count("DONE (verified)") == 1


def test_on_and_off_are_unchanged(arena, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_NET", "on")
    sync = Scripted(_arena(tmp_path, "on"), [FIX_X, DONE, DONE])
    assert sync.net_snapshot is None and sync.net_thread is None
    sync.run()
    assert sync.net_baseline == {X, Y}
    capsys.readouterr()                      # the `on` run's log is not the `off` run's

    monkeypatch.setenv("BEEKEEPER_NET", "off")
    off = Scripted(_arena(tmp_path, "off"), [FIX_X, DONE])
    assert off.net_cmd is None and off.net_snapshot is None and off.net_thread is None
    rc = off.run()
    out = capsys.readouterr().out
    assert rc == 0 and "net=off(env)" in out and "net baseline" not in out
