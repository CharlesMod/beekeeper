"""H-09, the regression net at done (harness-backlog.md; pool v2's iceberg).

The visible tests are named; the tests that decide are their siblings in the
same files. On iceberg the worker greened the three named cases, left four
siblings red in the same file, and called done. Laws:
  - the net command is the verify command with each named test replaced by
    the file it lives in (one entry per file); no named tests, no net;
  - the baseline — which tests in those files fail — is measured before the
    first edit or write, never assumed;
  - at done, after the visible verify passes, the net runs: a test that
    passed at baseline and fails now is a REGRESSION and done is refused
    outright; siblings that were red at baseline and are still red refuse
    done ONCE, by name, so the model looks at them — a re-issued done
    accepts them as pre-existing (override by repetition, like the numeric
    gate);
  - BEEKEEPER_NET=off disables the net, declared in the settings line.
"""
import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

RUNNER = textwrap.dedent('''
    import os, sys
    KNOWN = {"tests/test_a.py": ["test_x", "test_y", "test_z"], "tests/test_b.py": ["test_q"]}
    want = []
    for a in sys.argv[1:]:
        if "::" in a:
            f, n = a.split("::"); want.append((f, n))
        elif a in KNOWN:
            want += [(a, n) for n in KNOWN[a]]
    failed = [f"{f}::{n}" for f, n in want if not os.path.exists(f"pass_{n}")]
    for t in failed: print(f"FAILED {t} - boom")
    npass = len(want) - len(failed)
    print(f"{len(failed)} failed, {npass} passed" if failed else f"{npass} passed")
    sys.exit(1 if failed else 0)
''')
VERIFY = "python3 runtests.py tests/test_a.py::test_x"


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


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.delenv("BEEKEEPER_NET", raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "runtests.py").write_text(RUNNER)
    (a / "tests").mkdir()
    (a / "tests" / "test_a.py").write_text("# tests\n")
    (a / "pass_test_z").write_text("")          # z passes at baseline; x and y fail
    return a


FIX_X = ("write", {"file_path": "pass_test_x", "content": "fixed\n"})
BREAK_Z = ("bash", {"command": "rm -f pass_test_z"})
DONE = ("done", {"summary": "done"})


def test_net_command_is_the_verify_over_the_files_the_tests_live_in(arena):
    bk = Scripted(arena, [])
    assert bk.net_cmd == "python3 runtests.py tests/test_a.py"
    two = Scripted(arena, [], verify="python3 runtests.py 'tests/test_a.py::test_x' 'tests/test_a.py::test_y' tests/test_b.py::test_q")
    assert two.net_cmd == "python3 runtests.py tests/test_a.py tests/test_b.py"
    assert Scripted(arena, [], verify="sh -c 'test -f fixed'").net_cmd is None


def test_baseline_is_measured_before_the_first_edit(arena, capsys):
    bk = Scripted(arena, [FIX_X])
    bk.run()
    assert bk.net_baseline == {"tests/test_a.py::test_x", "tests/test_a.py::test_y"}
    assert "net baseline: 2 failing" in capsys.readouterr().out


def test_a_symptom_fix_is_refused_once_by_name_then_accepted(arena, capsys):
    rc = Scripted(arena, [FIX_X, DONE, DONE]).run()
    out = capsys.readouterr().out
    assert "done refused" in out and "test_y" in out, out
    assert rc == 0, "the second done accepts the pre-existing sibling"
    assert out.count("DONE (verified)") == 1


def test_a_regression_refuses_done_outright(arena, capsys):
    rc = Scripted(arena, [FIX_X, BREAK_Z, DONE, DONE]).run()
    out = capsys.readouterr().out
    assert rc != 0 and "DONE (verified)" not in out
    assert "regression" in out and "test_z" in out, out


def test_net_off_is_declared_and_skips_the_net(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_NET", "off")
    rc = Scripted(arena, [FIX_X, DONE]).run()
    out = capsys.readouterr().out
    assert rc == 0 and "net=off(env)" in out and "net baseline" not in out
