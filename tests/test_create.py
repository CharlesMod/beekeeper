"""H-56, missing file -> create (the review's L3): when the output the harness
just read shows an import of a path that does not exist in the arena, the
harness does not tell the model to write it — it OFFERS the write, restricted
to exactly the paths it derived.

The failure class (pool v2's four interface tasks): the visible test imports
`pkg.sub`, the module was never written, and the model spends the cap probing
for a file that is not there — reads, greps, `python -c` imports — because
`write` is a general affordance and nothing in the schema says WHICH path is
missing. An affordance naming the path is structural; a sentence saying
"create the missing module" is instructional, and instructions are what the
ring measured as inert. Laws:

  - off by default; `BEEKEEPER_CREATE=on` declares it, the settings line
    names it, and with it off the run is byte-identical (no tool, no note,
    no scan);
  - a `ModuleNotFoundError`, a pytest collection `ERROR ... No module named`,
    an `ImportError: cannot import name` naming a module that is itself
    absent, and a `FileNotFoundError` naming a path all yield candidates:
    `pkg/sub.py` and `pkg/sub/__init__.py` for a module, the path itself for
    a file — relative to the arena, only inside the arena, only when the
    import is rooted in the arena (a missing third-party package is a
    dependency, not a file to invent);
  - a missing NAME in a module that EXISTS is a different fault (the module
    is there; the symbol is not): it is recorded in the ledger and no create
    is offered;
  - the offer reaches the NEXT turn's schema, with a one-line note naming
    the candidates, and lasts until the path exists or three turns pass;
  - `create` writes one of the listed paths and refuses any other;
  - a created file clears the stall law's exhaustion, like any write;
  - the ledger carries one record per offer ({"kind": "create", turn,
    offered, taken}) and the end record counts offers and takes.
"""
import json
import os
import shlex
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402

MOD = "ModuleNotFoundError: No module named 'pkg.sub'"
COLLECT = ("ERROR collecting tests/test_widget.py\n"
           "ImportError while importing test module 'tests/test_widget.py'.\n"
           "E   ModuleNotFoundError: No module named 'pkg.widget'")


def boom(text):
    """A bash call whose output carries a traceback."""
    return ("bash", {"command": f"echo {shlex.quote(text)}; exit 1"})


def chatter(n):
    return ("bash", {"command": f"echo step {n}"})


class Scripted(Beekeeper):
    def __init__(self, arena, script, **kw):
        super().__init__(str(arena), "the task", **kw)
        self.script = list(script)
        self.schemas = []                    # the tool schema offered each turn

    def request(self):
        self.schemas.append(self._request_body()["tools"])
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
    monkeypatch.setenv("BEEKEEPER_CREATE", "on")
    monkeypatch.delenv("BEEKEEPER_SPEND_DIR", raising=False)
    a = tmp_path / "arena"
    (a / "pkg").mkdir(parents=True)
    (a / "pkg" / "__init__.py").write_text("")
    (a / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (a / "f.py").write_text("greeting = 'hello'\n")
    return a


def names(schema):
    return [t["function"]["name"] for t in schema]


def create_tool(schema):
    return next((t for t in schema if t["function"]["name"] == "create"), None)


def offered(schema):
    t = create_tool(schema)
    return t["function"]["parameters"]["properties"]["file_path"]["enum"] if t else None


def turns_offering(bk):
    return [i for i, s in enumerate(bk.schemas, 1) if "create" in names(s)]


def notes(bk):
    return [str(m.get("content")) for m in bk.messages
            if str(m.get("content") or "").startswith("[create offered")]


def records(tmp_path):
    files = list((tmp_path / "spend").glob("*.jsonl"))
    assert len(files) == 1, files
    return [json.loads(l) for l in files[0].read_text().splitlines() if l.strip()]


# --- the flag ---

def test_off_by_default_and_named_in_the_settings_line(arena, monkeypatch, capsys):
    monkeypatch.delenv("BEEKEEPER_CREATE")
    bk = Scripted(arena, [boom(MOD), chatter(2), chatter(3)])
    bk.run()
    assert bk.create_policy == "off"
    assert turns_offering(bk) == [], [names(s) for s in bk.schemas]
    assert notes(bk) == []
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "create=off(default)" in line, line


def test_on_is_declared_in_the_settings_line(arena, capsys):
    Scripted(arena, [])
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "create=on(env)" in line, line


# --- deriving the candidates ---

def test_a_missing_module_offers_exactly_its_two_paths_on_the_next_turn(arena):
    bk = Scripted(arena, [boom(MOD), chatter(2)])
    bk.run()
    assert "create" not in names(bk.schemas[0]), "the offer is made on the NEXT turn, never the one that read it"
    assert offered(bk.schemas[1]) == ["pkg/sub.py", "pkg/sub/__init__.py"], bk.schemas[1]
    note = notes(bk)
    assert len(note) == 1 and "\n" not in note[0], note
    assert "pkg/sub.py" in note[0] and "pkg/sub/__init__.py" in note[0], note[0]


def test_a_pytest_collection_error_is_read_the_same_way(arena):
    bk = Scripted(arena, [boom(COLLECT), chatter(2)])
    bk.run()
    assert offered(bk.schemas[1]) == ["pkg/widget.py", "pkg/widget/__init__.py"]


def test_a_missing_file_path_is_its_own_candidate(arena):
    bk = Scripted(arena, [boom("FileNotFoundError: [Errno 2] No such file or directory: 'data/config.json'"),
                          chatter(2)])
    bk.run()
    assert offered(bk.schemas[1]) == ["data/config.json"]


def test_a_missing_program_is_not_a_file_to_create(arena):
    """pool v2's python-not-found loop: `FileNotFoundError: ... 'python3'` is a
    broken environment, not a file the model should write."""
    bk = Scripted(arena, [boom("FileNotFoundError: [Errno 2] No such file or directory: 'python3'"), chatter(2)])
    bk.run()
    assert turns_offering(bk) == []


def test_a_foreign_package_is_a_dependency_not_a_file(arena):
    bk = Scripted(arena, [boom("ModuleNotFoundError: No module named 'sortedcontainers'"),
                          boom("ModuleNotFoundError: No module named 'numpy.linalg.blas'"), chatter(3)])
    bk.run()
    assert turns_offering(bk) == [], [names(s) for s in bk.schemas]


def test_a_path_that_already_exists_is_never_offered(arena):
    bk = Scripted(arena, [boom("ModuleNotFoundError: No module named 'pkg.mod'"), chatter(2)])
    bk.run()
    assert turns_offering(bk) == []


def test_a_missing_name_in_an_existing_module_is_recorded_not_offered(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [boom("ImportError: cannot import name 'Thing' from 'pkg.mod'"), chatter(2)])
    bk.run()
    assert turns_offering(bk) == [], "the module is there; the symbol is not — that is an edit, not a create"
    skips = [r for r in records(tmp_path) if r.get("kind") == "create_skip"]
    assert len(skips) == 1 and skips[0]["module"] == "pkg.mod" and skips[0]["name"] == "Thing", skips


def test_a_missing_name_from_a_module_that_is_absent_is_offered(arena):
    bk = Scripted(arena, [boom("ImportError: cannot import name 'Thing' from 'pkg.gone'"), chatter(2)])
    bk.run()
    assert offered(bk.schemas[1]) == ["pkg/gone.py", "pkg/gone/__init__.py"]


# --- the offer's life ---

def test_the_offer_lasts_three_turns_and_then_expires(arena):
    bk = Scripted(arena, [boom(MOD)] + [chatter(i) for i in range(2, 8)])
    bk.run()
    assert turns_offering(bk) == [2, 3, 4], [names(s) for s in bk.schemas]


def test_the_offer_ends_when_the_path_exists(arena):
    bk = Scripted(arena, [boom(MOD),
                          ("create", {"file_path": "pkg/sub/__init__.py", "content": "VALUE = 2\n"}),
                          chatter(3), chatter(4)])
    bk.run()
    assert (arena / "pkg" / "sub" / "__init__.py").read_text() == "VALUE = 2\n"
    assert turns_offering(bk) == [2], "the path exists now; the affordance goes away"


# --- create itself ---

def test_create_writes_a_listed_path_and_refuses_any_other(arena):
    bk = Scripted(arena, [boom(MOD),
                          ("create", {"file_path": "pkg/other.py", "content": "x = 1\n"}),
                          ("create", {"file_path": "pkg/sub.py", "content": "VALUE = 2\n"})])
    bk.run()
    assert not (arena / "pkg" / "other.py").exists(), "create is a write restricted to the listed paths"
    assert (arena / "pkg" / "sub.py").read_text() == "VALUE = 2\n"
    refusals = [str(m.get("content")) for m in bk.messages
                if m.get("role") == "tool" and str(m.get("content")).startswith("ERROR[blocked]")]
    assert len(refusals) == 1 and "pkg/sub.py" in refusals[0], refusals


def test_create_is_unknown_while_the_flag_is_off(arena, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_CREATE")
    bk = Scripted(arena, [("create", {"file_path": "pkg/sub.py", "content": "x = 1\n"})])
    bk.run()
    assert not (arena / "pkg" / "sub.py").exists()
    assert any("unknown tool create" in str(m.get("content")) for m in bk.messages if m.get("role") == "tool")


def test_create_makes_the_parent_directories(arena):
    bk = Scripted(arena, [boom("ModuleNotFoundError: No module named 'pkg.deep.down'"),
                          ("create", {"file_path": "pkg/deep/down.py", "content": "VALUE = 3\n"})])
    bk.run()
    assert (arena / "pkg" / "deep" / "down.py").read_text() == "VALUE = 3\n"


def test_a_created_file_clears_the_stall_laws_exhaustion(arena):
    A = ("bash", {"command": "echo run >> count.txt; echo same"})
    bk = Scripted(arena, [A, A, A, boom(MOD),
                          ("create", {"file_path": "pkg/sub.py", "content": "VALUE = 2\n"}), A])
    bk.run()
    assert (arena / "pkg" / "sub.py").exists()
    assert len((arena / "count.txt").read_text().splitlines()) == 4, \
        "the world changed: the exhausted probe is new information again"


# --- the ledger ---

def test_the_ledger_records_the_offer_and_the_end_record_counts_it(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [boom(MOD), ("create", {"file_path": "pkg/sub.py", "content": "VALUE = 2\n"}),
                          chatter(3)])
    bk.run()
    recs = records(tmp_path)
    offers = [r for r in recs if r.get("kind") == "create"]
    assert len(offers) == 1, recs
    assert offers[0]["turn"] == 1 and offers[0]["taken"] is True
    assert offers[0]["offered"] == ["pkg/sub.py", "pkg/sub/__init__.py"]
    assert offers[0]["arena"] == str(arena)
    end = [r for r in recs if r.get("kind") == "end"][0]
    assert end["create_offers"] == 1 and end["create_taken"] == 1


def test_an_offer_nobody_takes_is_recorded_as_untaken(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [boom(MOD)] + [chatter(i) for i in range(2, 8)])
    bk.run()
    offers = [r for r in records(tmp_path) if r.get("kind") == "create"]
    assert len(offers) == 1 and offers[0]["taken"] is False, offers
    end = [r for r in records(tmp_path) if r.get("kind") == "end"][0]
    assert end["create_offers"] == 1 and end["create_taken"] == 0


def test_a_live_offer_is_flushed_when_the_run_ends(arena, tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [boom(MOD), chatter(2)])
    bk.run()
    offers = [r for r in records(tmp_path) if r.get("kind") == "create"]
    assert len(offers) == 1 and offers[0]["taken"] is False, offers


def test_nothing_is_scanned_or_recorded_while_the_flag_is_off(arena, tmp_path, monkeypatch):
    monkeypatch.delenv("BEEKEEPER_CREATE")
    monkeypatch.setenv("BEEKEEPER_SPEND_DIR", str(tmp_path / "spend"))
    bk = Scripted(arena, [boom(MOD), boom("ImportError: cannot import name 'Thing' from 'pkg.mod'"), chatter(3)])
    bk.run()
    recs = records(tmp_path)
    assert [r for r in recs if r.get("kind") in ("create", "create_skip")] == []
    end = [r for r in recs if r.get("kind") == "end"][0]
    assert end["create_offers"] == 0 and end["create_taken"] == 0
