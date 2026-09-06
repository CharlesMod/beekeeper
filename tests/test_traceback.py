"""H-06, traceback capture: when a tool's output is too big to keep, keep the
part that says what broke.

Today bash keeps the first 3500 characters and drops the rest — and a pytest
run puts the assertion, the last frames of the traceback and the `FAILED …`
summary at the END, so the one thing the model needs is the one thing cut.
The verify has the mirror fault: it keeps the last 1500 characters and drops
the command, the collection line and the first error.

Laws:
  - off by default; `BEEKEEPER_TRACEBACK=on` declares it and the settings line
    names it with its source;
  - with the flag off every truncation is byte-identical to what it was;
  - with the flag on the same character budget buys head AND tail, with a
    marker naming how many lines were elided;
  - pytest's `E ` lines, its `FAILED …`/`ERROR …` summary rows and the last
    traceback frames never fall into the elided middle: if they sit there
    they are lifted out and kept.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    monkeypatch.delenv("BEEKEEPER_TRACEBACK", raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    return a


def _noise(n, tag="noise"):
    return [f"{tag} {i} " + "z" * 70 for i in range(n)]


def _pytest_output():
    """A run whose assertion and traceback sit in the middle: the FAILURES
    section first, then a long capture, then the summary."""
    return "\n".join(
        ["=" * 30 + " test session starts " + "=" * 30,
         "collected 412 items", ""]
        + _noise(30, "dot")
        + ["=" * 30 + " FAILURES " + "=" * 30,
           "____ test_widget ____",
           "Traceback (most recent call last):",
           '  File "/arena/tests/test_widget.py", line 42, in test_widget',
           "    assert widget.total() == 7",
           '  File "/arena/widget.py", line 118, in total',
           "    return self._sum / self.n",
           "E   ZeroDivisionError: division by zero",
           "E   assert 0 == 7"]
        + _noise(60, "captured-stdout")
        + ["=" * 30 + " short test summary info " + "=" * 30,
           "FAILED tests/test_widget.py::test_widget - ZeroDivisionError: division by zero",
           "1 failed, 411 passed in 3.21s"])


# ---------- the flag ----------

def test_off_by_default_and_named_on_the_settings_line(arena, capsys, monkeypatch):
    Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "traceback=off(default)" in line, line
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    bk = Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "traceback=on(env)" in line, line
    assert bk.traceback_policy == "on"


# ---------- the clipper itself ----------

def test_short_output_is_returned_unchanged():
    assert beekeeper.clip_output("hello\nworld", 3500) == "hello\nworld"


def test_the_clip_fits_its_budget_and_keeps_both_ends():
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    out = beekeeper.clip_output(text, 1000)
    assert len(out) <= 1000, len(out)
    assert out.startswith("FIRST LINE"), out[:120]
    assert out.endswith("LAST LINE"), out[-120:]
    assert "elided" in out, out


def test_the_failure_lines_never_fall_into_the_elided_middle():
    text = _pytest_output()
    out = beekeeper.clip_output(text, 1500)
    assert "E   ZeroDivisionError: division by zero" in out, out
    assert "E   assert 0 == 7" in out, out
    assert "FAILED tests/test_widget.py::test_widget" in out, out
    assert "1 failed, 411 passed in 3.21s" in out, out
    assert 'File "/arena/widget.py", line 118, in total' in out, out
    assert "Traceback (most recent call last):" in out, out
    assert len(out) <= 1500, len(out)


def test_the_clipper_is_the_only_thing_that_changes_the_bytes():
    """A run whose whole output fits is passed through untouched."""
    text = "\n".join(_pytest_output().splitlines()[:5])
    assert beekeeper.clip_output(text, 3500) == text


# ---------- bash ----------

def _run_big(arena, bk, text):
    (arena / "out.txt").write_text(text + "\n")
    return bk.t_bash("cat out.txt")


def test_flag_off_bash_truncation_is_byte_identical(arena):
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    bk = Beekeeper(str(arena), "t")
    got = _run_big(arena, bk, text)
    assert got == "exit 0\n" + text[:3500] + "\n... [truncated]", got[-200:]
    assert "LAST LINE" not in got


def test_flag_on_bash_keeps_the_head_and_the_tail(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    got = _run_big(arena, Beekeeper(str(arena), "t"), text)
    assert got.startswith("exit 0\nFIRST LINE"), got[:120]
    assert got.rstrip().endswith("LAST LINE"), got[-120:]
    assert "elided" in got
    assert len(got) <= 3500 + len("exit 0\n"), len(got)


def test_flag_on_a_pytest_run_keeps_its_assertion_and_summary(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    text = "\n".join(_pytest_output().splitlines() + _noise(400, "trailing"))
    got = _run_big(arena, Beekeeper(str(arena), "t"), text)
    assert "E   ZeroDivisionError: division by zero" in got, got
    assert "FAILED tests/test_widget.py::test_widget" in got, got


def test_flag_on_short_bash_output_is_untouched(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    assert Beekeeper(str(arena), "t").t_bash("echo hi") == "exit 0\nhi"


# ---------- the verify ----------

def _verify_cmd(arena, text):
    (arena / "v.txt").write_text(text + "\n")
    return "cat v.txt; exit 1"


def test_flag_off_the_verify_keeps_the_tail_alone(arena):
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    bk = Beekeeper(str(arena), "t", verify_cmd=_verify_cmd(arena, text), start_verify=False)
    code, out = bk._run_verify()
    assert code == 1
    assert out == (text + "\n")[-1500:], out[:120]
    assert "FIRST LINE" not in out


def test_flag_on_the_verify_keeps_its_head_too(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    bk = Beekeeper(str(arena), "t", verify_cmd=_verify_cmd(arena, text), start_verify=False)
    code, out = bk._run_verify()
    assert code == 1 and len(out) <= 1500, len(out)
    assert out.startswith("FIRST LINE") and "LAST LINE" in out, out[:120]


def test_flag_on_the_verify_still_counts_its_failures(arena, monkeypatch):
    """The clip must not cost the harness the number it scores with."""
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    text = "\n".join(_pytest_output().splitlines() + _noise(300, "trailing"))
    bk = Beekeeper(str(arena), "t", verify_cmd=_verify_cmd(arena, text), start_verify=False)
    code, out = bk._run_verify()
    assert bk.failing_count(out) == 1, out


# ---------- auto-verify ----------

def test_flag_on_the_autoverify_body_keeps_the_head(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    monkeypatch.setenv("BEEKEEPER_AUTOVERIFY", "on")
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    bk = Beekeeper(str(arena), "t", verify_cmd=_verify_cmd(arena, text), start_verify=False)
    bk._autoverify(1)
    body = [m for m in bk.messages if str(m.get("content", "")).startswith("[auto-verify")][0]["content"]
    assert "FIRST LINE" in body and "LAST LINE" in body, body[:200]


def test_flag_off_the_autoverify_body_is_the_tail_alone(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_AUTOVERIFY", "on")
    text = "\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"])
    bk = Beekeeper(str(arena), "t", verify_cmd=_verify_cmd(arena, text), start_verify=False)
    bk._autoverify(1)
    body = [m for m in bk.messages if str(m.get("content", "")).startswith("[auto-verify")][0]["content"]
    assert "FIRST LINE" not in body and "LAST LINE" in body, body[:200]


# ---------- H-51 / M-04: the counts reach the end record ----------

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


def _ends(tmp_path):
    recs = [json.loads(l) for f in sorted((tmp_path / "spend").glob("*.jsonl"))
            for l in f.read_text().splitlines() if l.strip()]
    return [r for r in recs if r.get("kind") == "end"]


CLIP_KEYS = {"truncations", "clips"}


def test_the_clip_counts_reach_the_end_records_opportunities(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_TRACEBACK", "on")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    (arena / "out.txt").write_text("\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"]) + "\n")
    bk = Scripted(arena, "t", [("bash", {"command": "cat out.txt"})])
    bk.run()
    o = _ends(tmp_path)[0]["opportunities"]
    assert CLIP_KEYS <= set(o), sorted(o)
    assert o["truncations"] == 1 and o["clips"] == 1, o


def test_the_truncation_gate_is_counted_with_the_flag_off(arena, tmp_path, monkeypatch):
    """Zero clips on a run that never overflowed and zero clips on a control
    arm are the same number until the gate is reported beside them."""
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    (arena / "out.txt").write_text("\n".join(["FIRST LINE"] + _noise(400) + ["LAST LINE"]) + "\n")
    bk = Scripted(arena, "t", [("bash", {"command": "cat out.txt"}),
                               ("bash", {"command": "echo small"})])
    bk.run()
    o = _ends(tmp_path)[0]["opportunities"]
    assert CLIP_KEYS <= set(o), sorted(o)
    assert o["truncations"] == 1, "the gate is counted whatever the policy says"
    assert o["clips"] == 0, o
