"""The net command is DERIVED, not spliced — SD-06 and the quoting.

Two faults found reading the ring's own verify commands (2026-09-06):

  1. Every SWE-rebench verify carries `-x`. The net runs that command over
     the whole FILES the named tests live in, so it stopped at the first
     failure: the baseline saw one failing sibling where five were failing,
     and the gate at `done` compared against a truncated set. SD-06's rule —
     the verify runs every named test so the failing count is a GRADIENT and
     not a bit — binds the net's own run too. `-x`, `--exitfirst` and
     `--maxfail` are stripped when the net command is derived, and the
     stripping is a ledger fact, not a silent rewrite.

  2. `_net_cmd` matched ids with a regex that stopped the NAME half at the
     first space, so a parametrised id quoted by shlex —
     'a.py::test_x[a b]' — lost its closing quote and left
     `'a.py b]'` in the command: a file that does not exist, and pytest's
     exit 4 (which, before H-09c, was read as a baseline of zero). The file
     is now taken from a properly tokenised id, split on the FIRST `::`.

tests/test_net.py is the pinned law (bare ids, one entry per file, the
dropped duplicate's whitespace, the `;` that ends a pytest command) and is
never edited; this file only adds.
"""
import json
import os
import shlex
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

# honours -x, so a net run that inherited it reports ONE failure of two
RUNNER = textwrap.dedent('''
    import os, sys
    KNOWN = {"tests/test_a.py": ["test_x", "test_y", "test_z"]}
    want, stop = [], False
    for a in sys.argv[1:]:
        if a in ("-x", "--exitfirst") or a.startswith("--maxfail"):
            stop = True
        elif "::" in a:
            f, n = a.split("::", 1); want.append((f, n))
        elif a in KNOWN:
            want += [(a, n) for n in KNOWN[a]]
    failed = [f"{f}::{n}" for f, n in want if not os.path.exists("pass_" + n)]
    if stop:
        failed = failed[:1]
    for t in failed: print("FAILED " + t + " - boom")
    npass = len(want) - len(failed)
    print(f"{len(failed)} failed, {npass} passed" if failed else f"{npass} passed")
    sys.exit(1 if failed else 0)
''')

X, Y = "tests/test_a.py::test_x", "tests/test_a.py::test_y"
VERIFY_X = "python3 runtests.py -q -x tests/test_a.py::test_x"
FIX_X = ("write", {"file_path": "pass_test_x", "content": "fixed\n"})
DONE = ("done", {"summary": "done"})


class Scripted(Beekeeper):
    def __init__(self, arena, script, verify):
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
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    for k in ("BEEKEEPER_NET", "BEEKEEPER_AUTOVERIFY", "BEEKEEPER_BOARD", "BEEKEEPER_RESTART"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "runtests.py").write_text(RUNNER)
    (a / "tests").mkdir()
    (a / "tests" / "test_a.py").write_text("# tests\n")
    (a / "pass_test_z").write_text("")          # z passes at baseline; x and y fail
    return a


def _baseline(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    recs = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    base = [r for r in recs if r.get("kind") == "net" and r.get("stage") == "baseline"]
    assert len(base) == 1, base
    return base[0]


# ---------- 1. SD-06: the net's run is a gradient, not a bit ----------

@pytest.mark.parametrize("flag", ["-x", "--exitfirst", "--maxfail=1", "--maxfail 2"])
def test_the_net_does_not_inherit_the_verifys_fail_fast(flag):
    cmd = f"python -m pytest -q {flag} -p no:cacheprovider tests/a.py::test_x"
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -q -p no:cacheprovider tests/a.py"


def test_a_flag_that_only_looks_like_one_is_left_alone():
    """`-rx` is pytest's report selector, not a fail-fast; a path may hold
    the letters. Only whole tokens are stripped."""
    cmd = "python -m pytest -rx --maxfailures=2 tests/a.py::test_x xx/-x.py::test_y"
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -rx --maxfailures=2 tests/a.py xx/-x.py"


def test_the_stripped_flags_are_a_ledger_fact(arena, tmp_path):
    bk = Scripted(arena, [FIX_X, DONE], verify=VERIFY_X)
    assert bk.net_cmd == "python3 runtests.py -q tests/test_a.py"
    bk.run()
    assert _baseline(tmp_path)["stripped"] == ["-x"], _baseline(tmp_path)


def test_a_verify_without_a_fail_fast_strips_nothing(arena, tmp_path):
    bk = Scripted(arena, [FIX_X, DONE], verify="python3 runtests.py -q tests/test_a.py::test_x")
    bk.run()
    assert _baseline(tmp_path)["stripped"] == [], _baseline(tmp_path)


def test_the_baseline_measures_every_sibling_not_just_the_first(arena, tmp_path):
    """The payoff, end to end: under `-x` the net run stopped at test_x and
    the baseline read one failing. Both are failing, and the gate needs both
    — test_y is exactly the pre-existing sibling H-09 exists to name."""
    bk = Scripted(arena, [FIX_X, DONE], verify=VERIFY_X)
    bk.run()
    assert bk.net_baseline == {X, Y}, bk.net_baseline
    assert _baseline(tmp_path)["failing"] == 2


# ---------- 2. the id is tokenised, not scraped ----------

def test_a_parametrised_id_with_a_space_keeps_its_file():
    cmd = "python -m pytest -q " + shlex.quote("tests/a.py::test_x[a b]")
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -q tests/a.py"


def test_spaced_and_plain_ids_together_collapse_to_one_file_each():
    ids = ["tests/a.py::test_x[a b]", "tests/a.py::test_y", "tests/b.py::test_q[1-2]"]
    cmd = "python -m pytest -q " + " ".join(shlex.quote(i) for i in ids) + "; echo done"
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -q tests/a.py tests/b.py; echo done"


def test_a_double_quoted_id_is_read_the_same_way():
    cmd = 'python -m pytest -q "tests/a.py::test_x[a b]" "tests/b.py::test_q"'
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -q tests/a.py tests/b.py"


def test_an_id_holding_a_quote_survives_shlexs_own_escape():
    """shlex.quote writes a `'` inside a single-quoted word as '"'"' — the
    id must come back whole, and its file with it."""
    raw = "tests/a.py::test_x[it's a name]"
    cmd = "python -m pytest -q " + shlex.quote(raw)
    assert shlex.split(cmd)[-1] == raw, "the fixture itself is the shlex escape"
    assert Beekeeper._net_cmd(cmd) == "python -m pytest -q tests/a.py"


def test_the_real_ring_command_with_a_spaced_parameter():
    """The shape that produced `'tests/other/test_util.py b]'` on pool v2:
    the docker wrapper, the inner bash -c, shlex-quoted ids."""
    ids = ["tests/other/test_util.py::test_format[2000-11-11T11:33:31Z-end5-0:01:02]",
           "tests/other/test_util.py::test_format_datetime[value1-%Y-%m-%d %H:%M:%SZ]"]
    inner = ('export PATH=/opt/bin:\\$PATH; cd /testbed && python -m pytest -q -x '
             '-p no:cacheprovider ' + " ".join(shlex.quote(i) for i in ids) +
             '; rc=\\$?; exit \\$rc')
    cmd = f'docker run --rm -v "$PWD":/testbed -w /testbed img bash -c "{inner}"'
    net = Beekeeper._net_cmd(cmd)
    assert "b]" not in net and "::" not in net and " -x " not in net, net
    assert net.count("tests/other/test_util.py") == 1, net
    assert net == cmd.replace(" -x", "").replace(
        " ".join(shlex.quote(i) for i in ids), "tests/other/test_util.py"), net


def test_a_command_naming_no_tests_is_still_no_net():
    assert Beekeeper._net_cmd("sh -c 'test -f fixed'") is None
    assert Beekeeper._net_cmd("python -m pytest -q -x tests/") is None
