"""The eviction policy (H-05, budget-law.md §5), behind BEEKEEPER_EVICT.

The 09-03 public ring assumed a 24,000-token context under a 131,072-token
server, so compaction fired every turn at a tenth of the room and did four
harmful things at once: it evicted the file the model was working on, it
UN-CACHED that file so the next read re-sent it whole, it reset the repeat
counter so the loop could never be seen, and it left an INSTRUCTION —
"re-run the tool if needed" — where a fact belonged. Sixty identical reads
followed. The budget was fixed; the policy was not.

`BEEKEEPER_EVICT=working-set` is the policy, off by default:
  - the working set — the files the task names, the last edited file, the
    last verify output's head and tail — is never evicted;
  - nothing is un-cached by eviction: the same file re-read afterwards is
    served from the cache the worker already keeps, never re-sent whole;
  - the placeholder left behind carries a ledger fact
    (`[read src/a.py, 312 lines, evicted at turn 14]`), never an instruction,
    and the compaction notice is ONE message rebuilt in place, not a new one
    every compaction (I7: a budget message is never an attractor);
  - the repeat counter and the collapse anchors survive compaction;
  - every compaction writes a spend-ledger record naming what was evicted
    and what was kept — under BOTH policies, so the control arm is readable
    (M-04: a lever whose gate was never reached is unmeasured, not inert).

Default (`basic`) is today's policy, byte for byte.
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

TASK = ("Fix the failure in src/target.py. The regression test lives in "
        "tests/test_target.py and it is the only thing that decides.")
# red at start, and its output is big enough to be evictable
# a big bash output, in the shell alone: an interpreter on PATH is exactly what
# the ring could not assume (pool v2, the environment-loop finding)
BIG = "head -c %d /dev/zero | tr '\\0' '%s'"
VERIFY = "pytest tests/test_target.py::test_t"
GREENISH = ("bash", {"command": "echo pytest-run; " + BIG % (3000, "G") + "; echo; echo TAILMARK"})


def flood(n):
    """A distinct, large bash output — repetition would collapse, not evict."""
    return ("bash", {"command": f"echo {n}; " + BIG % (1400, "z")})


class Scripted(Beekeeper):
    def __init__(self, arena, script, task=TASK, verify_cmd=None):
        super().__init__(str(arena), task, verify_cmd=verify_cmd, start_verify=False)
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
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "1400")   # small: compaction fires
    for k in ("BEEKEEPER_EVICT", "BEEKEEPER_SPEND_DIR", "BEEKEEPER_BOARD",
              "BEEKEEPER_AUTOVERIFY", "BEEKEEPER_THINK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BEEKEEPER_NET", "off")   # the net's baseline is another lever's clock
    a = tmp_path / "arena"
    (a / "src").mkdir(parents=True)
    (a / "tests").mkdir()
    for name in ("target", "other", "spare"):
        (a / "src" / f"{name}.py").write_text(
            "\n".join(f"{name}_line_{i} = {i}" for i in range(40)) + "\n")
    (a / "tests" / "test_target.py").write_text("def test_t():\n    assert False\n")
    return a


READ_T = ("read", {"file_path": "src/target.py"})
READ_O = ("read", {"file_path": "src/other.py"})
READ_S = ("read", {"file_path": "src/spare.py"})
# not numeric-only: the anti-Goodhart gate would refuse that one, and a refused
# edit is not an edit
EDIT_O = ("edit", {"file_path": "src/other.py",
                   "old_str": "other_line_3 = 3", "new_str": "other_line_3 = 3  # touched"})


def tool_texts(bk):
    return [str(m.get("content") or "") for m in bk.messages if m.get("role") == "tool"]


def run(arena, script, **kw):
    bk = Scripted(arena, script, **kw)
    bk.run()
    return bk


# ---------- the flag ----------

def test_the_flag_is_off_by_default_and_named_in_the_settings_line(arena, monkeypatch, capsys):
    bk = Beekeeper(str(arena), TASK)
    assert (bk.evict_policy, bk.evict_source) == ("basic", "default")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l]
    assert line and "evict=basic(default)" in line[0], line
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk2 = Beekeeper(str(arena), TASK)
    assert (bk2.evict_policy, bk2.evict_source) == ("working-set", "env")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l]
    assert line and "evict=working-set(env)" in line[0], line


def test_an_unrecognised_policy_falls_back_to_the_shipped_one(arena, monkeypatch):
    """A typo in an arm's JSON must not silently become a third policy."""
    script = [READ_T, READ_S] + [flood(i) for i in range(6)]
    off = run(arena, list(script))
    monkeypatch.setenv("BEEKEEPER_EVICT", "workingset")
    typo = run(arena, list(script))
    assert typo.evict_policy == "workingset"      # named honestly in the settings line
    assert [m["content"] for m in typo.messages] == [m["content"] for m in off.messages]


def test_flag_off_compaction_is_byte_identical_to_todays_policy(arena, monkeypatch):
    """The control must not move. Today's compaction, verbatim: the generic
    placeholder that tells the model to re-run the tool, the notice inserted
    fresh every time, and the read un-cached behind it."""
    script = [READ_T, READ_S] + [flood(i) for i in range(6)]
    off = run(arena, list(script))
    assert off.compactions >= 1, "the fixture must compact"
    assert "(evicted to fit context — re-run the tool if needed)" in tool_texts(off)
    notices = [str(m["content"]) for m in off.messages
               if m.get("role") == "user" and str(m["content"]).startswith("[context compacted:")]
    assert notices, off.messages
    assert notices[0].endswith("Do not re-read unchanged files.]"), notices[0]
    assert off.read_cache == {}, "today's policy un-caches every evicted read"

    monkeypatch.setenv("BEEKEEPER_EVICT", "basic")
    same = run(arena, list(script))
    assert [m["content"] for m in same.messages] == [m["content"] for m in off.messages]

    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    on = run(arena, list(script))
    assert [m["content"] for m in on.messages] != [m["content"] for m in off.messages], \
        "the lever must actually change the context"


# ---------- the working set ----------

def test_a_file_the_task_names_is_never_evicted(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_T, READ_S] + [flood(i) for i in range(6)])
    assert bk.compactions >= 1
    texts = tool_texts(bk)
    assert any("target_line_39 = 39" in t for t in texts), "the task's file left the context"
    assert any(re.search(r"\[read src/spare\.py, \d+ lines, evicted at turn \d+\]", t)
               for t in texts), texts


def test_the_last_edited_file_is_never_evicted(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_S, READ_O, EDIT_O] + [flood(i) for i in range(6)])
    assert bk.compactions >= 1
    texts = tool_texts(bk)
    assert any("other_line_39 = 39" in t for t in texts), "the file being edited left the context"
    assert any("evicted at turn" in t for t in texts), "nothing was evicted at all"


def test_the_last_verify_output_keeps_its_head_and_tail(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    monkeypatch.setenv("BEEKEEPER_BOARD", "on")   # the board inserts at 2 as well
    bk = run(arena, [GREENISH] + [flood(i) for i in range(7)], verify_cmd=VERIFY)
    assert bk.compactions >= 1
    texts = tool_texts(bk)
    assert any(t.startswith("exit 0") and t.rstrip().endswith("TAILMARK") for t in texts), \
        "the verify output lost its head or its tail"


# ---------- the cache ----------

def test_nothing_is_un_cached_by_eviction(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_S] + [flood(i) for i in range(6)])
    assert bk.compactions >= 1
    assert os.path.join(bk.arena, "src/spare.py") in bk.read_cache, \
        "eviction un-cached the read it evicted"


def test_a_re_read_after_eviction_is_served_from_the_cache(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_S] + [flood(i) for i in range(6)] + [READ_S])
    served = [t for t in tool_texts(bk) if t.startswith("[unchanged since your last read")]
    assert served, tool_texts(bk)
    assert "spare_line_39 = 39" not in served[-1], "the file was re-sent whole after eviction"
    assert re.search(r"read src/spare\.py, \d+ lines, evicted at turn \d+", served[-1]), served[-1]


# ---------- the placeholder ----------

def test_the_placeholder_carries_a_ledger_fact_never_an_instruction(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_S] + [flood(i) for i in range(6)])
    marks = [t for t in tool_texts(bk) if "evicted at turn" in t]
    assert marks, tool_texts(bk)
    for t in marks:
        low = t.lower()
        for word in ("re-run", "rerun", "if needed", "do not", "should", "please"):
            assert word not in low, (word, t)


def test_the_compaction_notice_is_one_message_rebuilt_in_place(arena, monkeypatch):
    """I7: a budget message is one entry in the context, never a pattern the
    model completes. Today's compaction inserts a new notice every time."""
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    bk = run(arena, [READ_S] + [flood(i) for i in range(10)])
    assert bk.compactions >= 2, bk.compactions
    notices = [m for m in bk.messages if m.get("role") == "user"
               and str(m.get("content") or "").startswith("[context compacted")]
    assert len(notices) == 1, [n["content"][:60] for n in notices]
    assert bk.messages.index(notices[0]) in bk.pin_idx


# ---------- the counter and the collapse anchors ----------

def test_the_repeat_counter_survives_compaction(arena, monkeypatch):
    """Pinned, both policies: the stall law's exhaustion is a fact about the
    world, not about the context, and compaction must not forget it."""
    for policy in ("basic", "working-set"):
        monkeypatch.setenv("BEEKEEPER_EVICT", policy)
        same = ("bash", {"command": "echo same; " + BIG % (1500, "s")})
        bk = run(arena, [same, same, same, same] + [flood(i) for i in range(4)])
        assert bk.compactions >= 1, (policy, bk.compactions)
        assert bk.exhausted, f"{policy}: the exhaustion was forgotten across compaction"


def test_the_collapse_state_survives_compaction(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    same = ("bash", {"command": "echo same; " + BIG % (1500, "s")})
    bk = run(arena, [flood(0), flood(1), same, same, flood(2), flood(3), same])
    assert bk.compactions >= 1
    assert bk.last_result != (None, None), "the repeat counter was reset by compaction"
    if bk.last_result_idx is not None:
        anchor = str(bk.messages[bk.last_result_idx].get("content") or "")
        assert "evicted at turn" not in anchor, "the collapse anchor was evicted"


# ---------- the ledger ----------

@pytest.mark.parametrize("policy", ["basic", "working-set"])
def test_every_compaction_writes_a_spend_record(arena, monkeypatch, tmp_path, policy):
    monkeypatch.setenv("BEEKEEPER_EVICT", policy)
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = run(arena, [READ_T, READ_S] + [flood(i) for i in range(6)])
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    recs = [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]
    comp = [r for r in recs if r.get("kind") == "compact"]
    assert len(comp) == bk.compactions, (len(comp), bk.compactions)
    r = comp[0]
    for k in ("turn", "hard", "policy", "evicted", "kept", "chars_before", "chars_after"):
        assert k in r, (k, r)
    assert r["policy"] == policy
    assert isinstance(r["evicted"], list) and isinstance(r["kept"], list)
    assert any(r["evicted"] for r in comp), comp


def test_a_refused_repeat_lands_on_its_own_message_after_a_compaction(arena, monkeypatch):
    """The other half of "the collapse state survives compaction": the
    surviving copy is addressed BY INDEX, and compaction inserts a message in
    front of it. An unshifted anchor rewrites the wrong message — the refusal
    note lands on an assistant turn and a real tool result is lost."""
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "4000")   # exhaust first, compact after
    same = ("bash", {"command": "echo same-probe"})
    bk = run(arena, [same, same, same] + [flood(i) for i in range(8)] + [same])
    assert bk.compactions >= 1, bk.compactions
    assert bk.refused_count, "the fixture must refuse the exhausted repeat"
    marked = [m for m in bk.messages
              if "this exact call has been made" in str(m.get("content") or "")]
    assert marked, [str(m.get("content"))[:60] for m in bk.messages]
    for m in marked:
        assert m.get("role") == "tool", (m.get("role"), str(m.get("content"))[:120])
    assert len(marked) == 1, len(marked)
    assert bk.messages.index(marked[0]) in bk.exhausted_idx.values()


def test_compaction_moves_every_index_the_harness_holds(arena, monkeypatch):
    """A message inserted at index 2 moves everything after it. Checked on the
    objects themselves: read_msgs, pin_idx, the collapse anchors and the verify
    must still name the messages they named before."""
    monkeypatch.setenv("BEEKEEPER_EVICT", "working-set")
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "200000")   # compacts only when told
    bk = run(arena, [READ_S, flood(0), flood(1)])
    assert bk.compactions == 0 and bk.read_msgs, (bk.compactions, bk.read_msgs)
    last = len(bk.messages) - 1
    bk.last_result_idx = bk.last_verify_idx = last
    bk.exhausted_idx = {"sig": last}
    was = {k: id(bk.messages[i]) for k, i in
           (("result", last), ("verify", last))}
    reads = {p: id(bk.messages[i]) for i, p in bk.read_msgs.items()}
    pins = {id(bk.messages[i]) for i in bk.pin_idx}
    bk.compact()
    assert bk.compactions == 1 and len(bk.messages) == last + 2
    assert id(bk.messages[bk.last_result_idx]) == was["result"]
    assert id(bk.messages[bk.last_verify_idx]) == was["verify"]
    assert id(bk.messages[bk.exhausted_idx["sig"]]) == was["result"]
    for i, path in bk.read_msgs.items():
        assert id(bk.messages[i]) == reads[path], path
    assert pins <= {id(bk.messages[i]) for i in bk.pin_idx}
