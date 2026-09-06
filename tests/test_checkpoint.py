"""H-03, the checkpoint law INSIDE the worker (a port of hive's
`quorum/checkpoint.py`, which the ring's bench episodes deliberately never
run — so the largest measured agentic failure class, the agent destroying
its own already-correct edits, is uncured exactly where it is measured).

Laws (BEEKEEPER_CHECKPOINT=on; off by default, the run byte-identical when
off):

  - off by default; `on` is declared in the settings line with its source,
    exactly as board/trend/net do;
  - a SHADOW ledger over the arena: a detached git dir (`--git-dir` /
    `--work-tree`) in a tempdir OUTSIDE the arena, so an arena that is
    itself a git checkout keeps its own HEAD, its own history and its own
    index untouched;
  - every MEASURED verify is scored with the worker's existing scorer
    (exit 0 is green; pytest's "N failed"; an opaque red is 1) and a state
    whose failing count strictly improves on the best is committed;
  - M-02: a verify that measured nothing (exit 4/5, "no tests ran") is
    unmeasured — never scored, never zero, never a checkpoint;
  - on any end that is NOT a green cap the best committed state is
    restored: best-green, else best-partial, else the scored baseline;
  - a green cap is never restored over — the tree the referee sees is the
    tree the model capped on;
  - the spend ledger carries `{"kind": "checkpoint", ...}` records and the
    end record a `restored` field; the transcript says what was restored
    and from which state.
"""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper, _Ledger  # noqa: E402

IDS = "tests/t.py::a tests/t.py::b"
# the start verify: the tree as it stands is red, two failing
RED2 = ("sh -c 'echo \"FAILED tests/t.py::a - boom\"; echo \"FAILED tests/t.py::b - boom\"; "
        "echo \"2 failed in 0.1s\"; exit 1' -- " + IDS)
# the model's own verifies; each command differs so no repeat guard collapses them
V_GREEN = ("bash", {"command": "sh -c 'echo \"2 passed in 0.2s\"; exit 0' -- pytest " + IDS})
V_HALF = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - boom\"; "
                              "echo \"1 failed, 1 passed in 0.3s\"; exit 1' -- pytest " + IDS})
V_RED2 = ("bash", {"command": "sh -c 'echo \"FAILED tests/t.py::a - back\"; "
                              "echo \"FAILED tests/t.py::b - back\"; "
                              "echo \"2 failed in 0.4s\"; exit 1' -- pytest " + IDS})
# M-02: pytest could not collect — this run measured nothing at all
V_UNMEASURED = ("bash", {"command": "sh -c 'echo \"usage: pytest: error: unrecognized\"; "
                                    "exit 4' -- pytest " + IDS})

ORIG = "greeting = 'hello'\n"
GOOD = "greeting = 'GOOD'\n"
HALF = "greeting = 'HALF'\n"
BAD = "greeting = 'BAD'\n"

W_GOOD = ("write", {"file_path": "f.py", "content": GOOD})
W_HALF = ("write", {"file_path": "f.py", "content": HALF})
W_BAD = ("write", {"file_path": "f.py", "content": BAD})

# the cap's own gate: a verify that actually reads the tree
GATE = ("sh -c 'if grep -q GOOD f.py; then echo \"2 passed in 0.1s\"; exit 0; "
        "else echo \"FAILED tests/t.py::a - boom\"; echo \"1 failed, 1 passed in 0.1s\"; "
        "exit 1; fi' -- pytest " + IDS)


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
    monkeypatch.setenv("BEEKEEPER_CHECKPOINT", "on")
    for k in ("BEEKEEPER_BOARD", "BEEKEEPER_TREND", "BEEKEEPER_LADDER", "BEEKEEPER_RESTART",
              "BEEKEEPER_CREATE", "BEEKEEPER_THINK", "BEEKEEPER_ALT", "BEEKEEPER_PHASE",
              "BEEKEEPER_AUTOVERIFY", "BEEKEEPER_NET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEEKEEPER_NET", "off")
    a = tmp_path / "arena"
    a.mkdir()
    (a / "f.py").write_text(ORIG)
    return a


def records(tmp_path, kind="checkpoint"):
    return [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()
            and json.loads(l).get("kind") == kind]


def settings(capsys):
    return [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]


# --- the flag ---

def test_off_by_default_and_named_in_the_settings_line(arena, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BEEKEEPER_CHECKPOINT")
    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()
    assert bk.checkpoint_policy == "off"
    assert "checkpoint=off(default)" in settings(capsys)
    assert bk.ckpt is None
    # byte-identical: the wreck stands, nothing was committed, nothing restored
    assert (arena / "f.py").read_text() == BAD
    assert not (arena / ".git").exists()
    assert records(tmp_path) == []
    assert records(tmp_path, "end")[0].get("restored") in (None, False)


def test_on_is_declared_in_the_settings_line(arena, capsys):
    Scripted(arena, [])
    assert "checkpoint=on(env)" in settings(capsys)


# --- the scorer: the worker's own, not a second one ---

@pytest.mark.parametrize("code,out,want", [
    (0, "", 0),                                   # exit 0 IS green
    (0, "2 passed in 0.1s", 0),
    (1, "3 failed, 2 passed in 0.2s", 3),         # pytest's count
    (1, "boom: Traceback", 1),                    # an opaque red scores 1
    (4, "usage: pytest: error: unrecognized", None),   # M-02: unmeasured
    (5, "no tests ran in 0.01s", None),
    (1, "no tests ran in 0.01s", None),
])
def test_the_score_is_the_workers_own_and_m02_is_never_zero(arena, code, out, want):
    assert Scripted(arena, [])._ckpt_score(code, out) == want


# --- the law ---

def test_a_run_that_wrecks_its_own_green_ends_at_its_best(arena, tmp_path, capsys):
    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()
    assert (arena / "f.py").read_text() == GOOD, "the best committed state was not restored"
    rest = [r for r in records(tmp_path) if r.get("stage") == "restore"]
    assert len(rest) == 1 and rest[0]["restored"] is True, rest
    assert rest[0]["green"] is True and rest[0]["failing"] == 0 and rest[0]["was_failing"] == 2
    assert rest[0]["sha"]
    end = records(tmp_path, "end")[0]
    assert end["restored"] and end["restored"]["failing"] == 0
    out = capsys.readouterr().out
    assert "checkpoint" in out and "restored" in out, out


def test_the_best_partial_is_restored_when_no_green_was_ever_reached(arena, tmp_path):
    bk = Scripted(arena, [W_HALF, V_HALF, W_BAD, V_RED2])
    bk.run()
    assert (arena / "f.py").read_text() == HALF
    rest = [r for r in records(tmp_path) if r.get("stage") == "restore"][0]
    assert rest["restored"] is True and rest["failing"] == 1 and rest["green"] is False


def test_a_green_cap_is_never_restored_over(arena, tmp_path):
    """The tree the referee sees is the tree the model capped on."""
    LATER = GOOD + "extra = 1\n"
    bk = Scripted(arena, [W_GOOD, V_GREEN,
                          ("write", {"file_path": "f.py", "content": LATER}),
                          ("done", {"summary": "fixed"})], verify_cmd=GATE)
    rc = bk.run()
    assert rc == 0 and bk.end_reason == "capped"
    assert (arena / "f.py").read_text() == LATER, "a green cap was rolled back"
    rest = [r for r in records(tmp_path) if r.get("stage") == "restore"]
    assert rest and rest[0]["restored"] is False, rest
    assert records(tmp_path, "end")[0].get("restored") in (None, False)


def test_an_unmeasured_verify_is_never_a_checkpoint(arena, tmp_path):
    """M-02: exit 4 proves nothing about the tree — no commit, no score."""
    bk = Scripted(arena, [W_BAD, V_UNMEASURED])
    bk.run()
    commits = [r for r in records(tmp_path) if r.get("stage") == "commit"]
    assert [c["failing"] for c in commits] == [2], commits
    assert bk.ckpt.n_scored == 1


def test_the_arena_may_already_be_a_git_repo(arena, tmp_path):
    """The shadow ledger never touches a repo the task itself uses."""
    def git(*a):
        return subprocess.run(["git", "-C", str(arena), *a], capture_output=True,
                              text=True, check=True).stdout.strip()
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-q", "-m", "the task's own history")
    head, log_n = git("rev-parse", "HEAD"), git("rev-list", "--count", "HEAD")

    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()

    assert (arena / "f.py").read_text() == GOOD
    assert not bk.ckpt.git_dir.startswith(bk.arena + os.sep), bk.ckpt.git_dir
    assert git("rev-parse", "HEAD") == head, "the ledger committed into the task's own repo"
    assert git("rev-list", "--count", "HEAD") == log_n
    assert git("diff", "--cached", "--name-only") == "", "the ledger staged into the task's index"
    # the restored content is the only trace: an UNSTAGED modification of f.py
    assert git("status", "--porcelain").split() == ["M", "f.py"]


def test_the_ledger_is_removed_at_the_end(arena):
    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()
    assert not os.path.exists(bk.ckpt.git_dir)


def test_the_opportunity_counts_are_reported(arena, tmp_path):
    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()
    o = records(tmp_path, "end")[0]["opportunities"]
    assert o["checkpoint_scores"] == 3 and o["checkpoints"] == 2
    assert o["checkpoint_restores"] == 1


# --- the mutation proof ---

def test_mutation_the_restore_law_is_what_the_tests_measure(arena, monkeypatch):
    """Break the law — restore_best becomes a no-op — and the wreck survives.
    A test that cannot fail is worse than a red one."""
    monkeypatch.setattr(_Ledger, "restore_best", lambda self: {"restored": False})
    bk = Scripted(arena, [W_GOOD, V_GREEN, W_BAD, V_RED2])
    bk.run()
    assert (arena / "f.py").read_text() == BAD
