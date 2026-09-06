"""H-04, the symbol map: a read of a big file returns its shape, not 150 head
plus 40 tail lines of it.

The failure class (harness-review.md, the read loop): pygraphistry's 306 KB
file re-served whole turn after turn; montepy read one file sixty times. The
head+tail serve answers "what is in this file?" with the two ends of it — the
middle, where the symbol the model is hunting lives, is exactly what is cut.
A ~1 KB map of defs, classes and line ranges answers the question AND names
the next call: a ranged read that returns exactly those lines.

Laws:
  - off by default; `BEEKEEPER_SYMBOLMAP=on` declares it and
    `BEEKEEPER_SYMBOLMAP_CHARS` the threshold, both named on the settings
    line with their source;
  - with the flag off the read tool's output and its schema are unchanged,
    byte for byte;
  - over the threshold with the flag on: a symbol map, under ~1.3 KB, naming
    top-level defs and classes with line ranges and the methods inside a
    class, and telling the model how to ask for a range;
  - stdlib `ast` for Python, a regex fallback for every other language and
    for Python that will not parse; a file with no symbols keeps head+tail
    (a map of nothing is worse than the two ends);
  - a ranged read returns EXACTLY those lines, numbered, and is not blocked
    by the unchanged-since-your-last-read cache: the map is not the file.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import beekeeper  # noqa: E402
from beekeeper import Beekeeper  # noqa: E402


def _big_py(n_classes=6, n_methods=8):
    """A file that is unmistakably over the threshold and whose symbols sit in
    the middle, where head+tail cannot reach them."""
    out = ["import os", "import sys", "", "CONST = 1", ""]
    for c in range(n_classes):
        out.append(f"class Widget{c}:")
        out.append(f'    """Widget number {c}."""')
        for m in range(n_methods):
            out.append(f"    def method_{c}_{m}(self, a, b):")
            out.append(f"        # {'filler ' * 24}")
            out.append(f"        return a + b + {m}")
            out.append("")
    for f in range(4):
        out.append(f"def helper_{f}(x):")
        out.append(f"    # {'padding ' * 24}")
        out.append(f"    return x * {f}")
        out.append("")
    return "\n".join(out) + "\n"


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    for k in ("BEEKEEPER_SYMBOLMAP", "BEEKEEPER_SYMBOLMAP_CHARS"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    (a / "big.py").write_text(_big_py())
    (a / "small.py").write_text("def f():\n    return 1\n")
    return a


def _head_tail(body):
    """The shape the worker has served since before this lever existed."""
    lines = body.splitlines()
    return "\n".join(lines[:150]
                     + [f"... [{len(lines) - 190} lines omitted — use edit for targeted changes] ..."]
                     + lines[-40:])


# ---------- the flag ----------

def test_off_by_default_and_named_on_the_settings_line(arena, capsys, monkeypatch):
    Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "symbolmap=off(default)" in line, line
    assert "symbolmap_chars=9000(default)" in line, line
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP_CHARS", "4000")
    bk = Beekeeper(str(arena), "t")
    line = [l for l in capsys.readouterr().out.splitlines() if "settings:" in l][0]
    assert "symbolmap=on(env)" in line, line
    assert "symbolmap_chars=4000(env)" in line, line
    assert bk.symbolmap_policy == "on" and bk.symbolmap_chars == 4000


# ---------- the flag OFF: nothing moves ----------

def test_flag_off_serves_head_and_tail_byte_identical(arena):
    bk = Beekeeper(str(arena), "t")
    body = (arena / "big.py").read_text()
    assert len(body) > 9000
    assert bk.t_read("big.py") == _head_tail(body)


def test_flag_off_the_read_schema_has_no_range(arena):
    props = [t for t in Beekeeper(str(arena), "t").tools()
             if t["function"]["name"] == "read"][0]["function"]["parameters"]["properties"]
    assert set(props) == {"file_path"}, props


def test_flag_off_a_range_is_refused_as_an_argument_error(arena):
    r = Beekeeper(str(arena), "t").t_read("big.py", start_line=1, end_line=2)
    assert r.startswith("ERROR[args]"), r


def test_flag_off_the_unchanged_note_is_the_old_words(arena):
    bk = Beekeeper(str(arena), "t")
    bk.t_read("big.py")
    assert bk.t_read("big.py") == "[unchanged since your last read — you already have this file in context]"


# ---------- the flag ON ----------

def test_a_big_file_reads_as_a_symbol_map(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    out = Beekeeper(str(arena), "t").t_read("big.py")
    assert "omitted — use edit for targeted changes" not in out, out
    assert len(out) <= 1300, len(out)
    assert "symbol map" in out
    assert "class Widget0" in out and "class Widget5" in out
    assert "helper_0" in out
    assert "method_0_0" in out          # the middle, which head+tail never reached
    assert "start_line" in out and "end_line" in out   # the map names the next call
    body = (arena / "big.py").read_text()
    assert str(len(body.splitlines())) in out          # the file's true length


def test_the_map_carries_line_ranges_that_locate_the_symbol(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    out = bk.t_read("big.py")
    lines = (arena / "big.py").read_text().splitlines()
    want = [i + 1 for i, l in enumerate(lines) if l.startswith("class Widget3:")][0]
    row = [l for l in out.splitlines() if "class Widget3" in l][0]
    assert str(want) in row, (row, want)


def test_a_range_read_returns_exactly_those_lines(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    lines = (arena / "big.py").read_text().splitlines()
    out = bk.t_read("big.py", start_line=40, end_line=44)
    assert out == "\n".join(f"{i}|{lines[i - 1]}" for i in range(40, 45)), out


def test_a_range_read_is_not_blocked_by_the_unchanged_cache(arena, monkeypatch):
    """The map is not the file: having served one, the harness must not tell
    the model it already has what it never got."""
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    bk.t_read("big.py")
    out = bk.t_read("big.py", start_line=10, end_line=12)
    assert "unchanged" not in out and out.startswith("10|"), out


def test_re_reading_a_mapped_file_points_at_the_range(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    bk.t_read("big.py")
    again = bk.t_read("big.py")
    assert "start_line" in again and "symbol map" in again, again


def test_the_read_schema_declares_the_range_when_on(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    fn = [t for t in Beekeeper(str(arena), "t").tools()
          if t["function"]["name"] == "read"][0]["function"]
    assert set(fn["parameters"]["properties"]) == {"file_path", "start_line", "end_line"}
    assert fn["parameters"]["required"] == ["file_path"]
    assert "range" in fn["description"] or "symbol map" in fn["description"]
    # the module-level schema is never mutated: the flag-off worker must not see this
    base = [t for t in beekeeper.TOOLS if t["function"]["name"] == "read"][0]
    assert set(base["function"]["parameters"]["properties"]) == {"file_path"}


def test_a_small_file_is_unchanged_with_the_flag_on(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    on = Beekeeper(str(arena), "t").t_read("small.py")
    monkeypatch.delenv("BEEKEEPER_SYMBOLMAP")
    assert on == Beekeeper(str(arena), "t").t_read("small.py") == "1|def f():\n2|    return 1"


def test_the_threshold_is_the_declared_one(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP_CHARS", "100000")
    body = (arena / "big.py").read_text()
    assert Beekeeper(str(arena), "t").t_read("big.py") == _head_tail(body)


# ---------- the fallbacks ----------

def test_a_non_python_file_maps_by_regex(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    js = ["// header"]
    for i in range(40):
        js.append(f"export function handle{i}(req, res) {{")
        js.append(f"  // {'x' * 90}")
        js.append(f"  // {'x' * 90}")
        js.append("}")
    js.append("class Router {")
    js.append("  // " + "y" * 90)
    js.append("}")
    (arena / "app.js").write_text("\n".join(js) + "\n")
    out = Beekeeper(str(arena), "t").t_read("app.js")
    assert "symbol map" in out and "handle0" in out and "Router" in out, out


def test_unparsable_python_still_maps(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    broken = _big_py() + "\ndef oops(:\n"
    (arena / "broken.py").write_text(broken)
    out = Beekeeper(str(arena), "t").t_read("broken.py")
    assert "symbol map" in out and "Widget0" in out, out


def test_a_file_with_no_symbols_keeps_head_and_tail(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    data = "\n".join(f"row,{i}," + "d" * 60 for i in range(400)) + "\n"
    (arena / "data.csv").write_text(data)
    assert Beekeeper(str(arena), "t").t_read("data.csv") == _head_tail(data)


def test_the_map_is_bounded_on_a_huge_file(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    (arena / "huge.py").write_text(_big_py(n_classes=60, n_methods=12))
    out = Beekeeper(str(arena), "t").t_read("huge.py")
    assert len(out) <= 1300, len(out)
    assert "more" in out, out          # the fold is declared, not silent


def test_a_range_read_cannot_re_serve_the_whole_file(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    out = bk.t_read("big.py", start_line=1, end_line=100000)
    assert len(out.splitlines()) <= beekeeper.SYMBOLMAP_RANGE_LINES + 1, len(out.splitlines())
    assert "1|import os" in out


def test_a_bad_range_is_an_argument_error(arena, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_SYMBOLMAP", "on")
    bk = Beekeeper(str(arena), "t")
    assert bk.t_read("big.py", start_line=90, end_line=10).startswith("ERROR[args]")
    assert bk.t_read("big.py", start_line=10 ** 7, end_line=10 ** 7 + 5).startswith("ERROR[args]")
