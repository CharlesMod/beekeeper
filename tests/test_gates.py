"""Gate tests for beekeeper's edit tool. Run: python3 -m pytest

The numeric-literal gate is the load-bearing one: in harness benchmarks, three
different scaffolds converged on the identical cheat (retuning a constant to
mask a planted leak) despite explicit anti-Goodhart prompting. Advisory text
does not deter a small model; only blocking gates do.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper

SOURCE = "RATE = 0.25\n\ndef drain(level):\n    return level - RATE\n"


def keeper(tmp_path, source=SOURCE):
    f = tmp_path / "machine.py"
    f.write_text(source)
    return Beekeeper(str(tmp_path), "fix the machine"), f


def test_numeric_only_edit_refused_once(tmp_path):
    bk, f = keeper(tmp_path)
    out = bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    assert out.startswith("ERROR[blocked]")
    assert f.read_text() == SOURCE  # refusal happens before the write


def test_refusal_names_the_rule_and_the_override(tmp_path):
    bk, _ = keeper(tmp_path)
    out = bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    assert "OPERATION" in out
    assert "sign, comparison, name" in out
    assert "re-issue this exact edit" in out
    assert "deliberate" in out


def test_identical_reissue_overrides(tmp_path):
    bk, f = keeper(tmp_path)
    bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    out = bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    assert out.startswith("OK: replaced 1 occurrence")
    assert "NOTE" in out and "numeric literals" in out
    assert "RATE = 0.1125" in f.read_text()


def test_override_is_consumed_not_standing(tmp_path):
    bk, f = keeper(tmp_path)
    bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")  # override applies
    f.write_text(SOURCE)  # revert out-of-band
    out = bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    assert out.startswith("ERROR[blocked]")  # must be re-earned


def test_pending_state_is_per_edit_not_global(tmp_path):
    bk, f = keeper(tmp_path)
    bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    out = bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.5")
    assert out.startswith("ERROR[blocked]")  # a different numeric edit gets its own refusal
    assert f.read_text() == SOURCE


def test_pending_state_is_per_file(tmp_path):
    (tmp_path / "machine.py").write_text(SOURCE)
    (tmp_path / "other.py").write_text(SOURCE)
    bk = Beekeeper(str(tmp_path), "fix the machines")
    bk.t_edit("machine.py", "RATE = 0.25", "RATE = 0.1125")
    out = bk.t_edit("other.py", "RATE = 0.25", "RATE = 0.1125")
    assert out.startswith("ERROR[blocked]")  # same strings, different file: own refusal


def test_operation_fix_passes_untouched(tmp_path):
    bk, f = keeper(tmp_path)
    out = bk.t_edit("machine.py", "return level - RATE", "return level + RATE")
    assert out == "OK: replaced 1 occurrence"
    assert "level + RATE" in f.read_text()


def test_mixed_edit_passes_untouched(tmp_path):
    bk, f = keeper(tmp_path)
    out = bk.t_edit("machine.py", "return level - RATE", "return level - RATE * 2")
    assert out == "OK: replaced 1 occurrence"
    assert "RATE * 2" in f.read_text()


def test_gate_applies_after_edit_grace(tmp_path):
    # copied N| read-prefixes are graced away; the resolved edit is still numeric-only
    bk, f = keeper(tmp_path)
    out = bk.t_edit("machine.py", "1|RATE = 0.25", "1|RATE = 0.1125")
    assert out.startswith("ERROR[blocked]")
    assert f.read_text() == SOURCE
    out = bk.t_edit("machine.py", "1|RATE = 0.25", "1|RATE = 0.1125")
    assert out.startswith("OK: replaced 1 occurrence")  # same raw strings resolve to the same key
    assert "RATE = 0.1125" in f.read_text()
