"""The context budget is measured, not assumed.

The 09-03 public ring: beekeeper assumed 24,000 tokens while the pinned
server offered 131,072, so it compacted at a tenth of its room — evicting
the working file every turn, un-caching it, resetting its own repeat
counter and printing "re-run the tool if needed". Sixty identical reads.
The fix starts with the number: the operator's word (env), then the
machine's standing config (~/.beekeeper.json), then the server's own
/props, then the old default — and the startup line names which one won,
so a transcript proves what budget a run had instead of asserting it.
"""
import http.server
import json
import os
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class _Props(http.server.BaseHTTPRequestHandler):
    n_ctx = 65536
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/props":
            body = json.dumps({"default_generation_settings": {"n_ctx": self.n_ctx},
                               "total_slots": 4}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture
def props_server():
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), _Props)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/v1"
    srv.shutdown()


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("BEEKEEPER_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))   # no ~/.beekeeper.json
    (tmp_path / "home").mkdir()
    return tmp_path


def _expect(bk, tokens):
    assert bk.compact_at == int(tokens * 0.55) * 4
    assert bk.hard_limit == int(tokens * 0.80) * 4


def test_env_is_the_operators_word_and_wins(clean_env, monkeypatch, props_server):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "131072")
    (clean_env / "home" / ".beekeeper.json").write_text(json.dumps({"context_budget_tokens": 40000}))
    bk = Beekeeper(str(clean_env), "task", base_url=props_server)
    _expect(bk, 131072)
    assert bk.budget_source == "env"


def test_file_outranks_the_server(clean_env, props_server):
    (clean_env / "home" / ".beekeeper.json").write_text(json.dumps({"context_budget_tokens": 40000}))
    bk = Beekeeper(str(clean_env), "task", base_url=props_server)
    _expect(bk, 40000)
    assert bk.budget_source == "file"


def test_server_props_is_the_measured_budget(clean_env, props_server):
    bk = Beekeeper(str(clean_env), "task", base_url=props_server)
    _expect(bk, 65536)
    assert bk.budget_source == "server"


def test_a_dead_server_falls_back_fast_to_the_old_default(clean_env):
    t0 = time.monotonic()
    bk = Beekeeper(str(clean_env), "task", base_url=f"http://127.0.0.1:{_free_port()}/v1")
    assert time.monotonic() - t0 < 3.0, "the probe must not stall a run"
    _expect(bk, 24000)
    assert bk.budget_source == "default"


def test_startup_line_names_the_budget_and_its_source(clean_env, props_server, capsys):
    Beekeeper(str(clean_env), "task", base_url=props_server)
    out = capsys.readouterr().out
    assert "context budget" in out and "65536" in out and "server" in out, out
