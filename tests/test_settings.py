"""Silent defaults in the worker, made explicit (silent-defaults.md). Laws:
  - the bash timeout covers a verify: BEEKEEPER_BASH_TIMEOUT, default 180 s
    (the ring admits verifies up to 120 s; the old 60 s killed a docker
    verify and then quarantined it after two strikes);
  - thinking unset means OFF, sent explicitly — never the server's choice;
  - the worker's temperature is BEEKEEPER_TEMPERATURE, default 0: the benched
    worker and the shipped worker are the same worker;
  - the transcript's first lines carry one `settings:` line naming every
    setting and its source.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from beekeeper import Beekeeper  # noqa: E402


@pytest.fixture
def arena(tmp_path, monkeypatch):
    monkeypatch.setenv("BEEKEEPER_CONTEXT_TOKENS", "24000")
    for k in ("BEEKEEPER_THINK", "BEEKEEPER_BASH_TIMEOUT", "BEEKEEPER_TEMPERATURE"):
        monkeypatch.delenv(k, raising=False)
    a = tmp_path / "arena"
    a.mkdir()
    return a


def test_bash_timeout_covers_a_verify(arena, monkeypatch):
    bk = Beekeeper(str(arena), "t")
    assert bk.bash_timeout == 180
    monkeypatch.setenv("BEEKEEPER_BASH_TIMEOUT", "240")
    assert Beekeeper(str(arena), "t").bash_timeout == 240


def test_thinking_unset_is_explicit_off(arena):
    bk = Beekeeper(str(arena), "t")
    body = bk._request_body()
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert bk.think_policy == "off"


def test_temperature_defaults_to_greedy_and_is_declared(arena, monkeypatch):
    assert Beekeeper(str(arena), "t")._request_body()["temperature"] == 0.0
    monkeypatch.setenv("BEEKEEPER_TEMPERATURE", "0.2")
    assert Beekeeper(str(arena), "t")._request_body()["temperature"] == 0.2


def test_the_settings_line_names_every_setting_and_its_source(arena, monkeypatch, capsys):
    monkeypatch.setenv("BEEKEEPER_THINK", "phase")
    Beekeeper(str(arena), "t")
    out = capsys.readouterr().out
    line = [l for l in out.splitlines() if "settings:" in l]
    assert line, out
    for k in ("budget=24000(env)", "think=phase(env)", "temperature=0(default)", "bash_timeout=180(default)",
              "max_turns=60", "think_budget=512", "think_ceiling=4096"):
        assert k in line[0], (k, line[0])
