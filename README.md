# 🐝 beekeeper

A terse, honest coding agent harness in ~450 lines of stdlib Python.
It fixes code with the fewest causal moves — and **it cannot lie about
being done**: completion is gated on your test suite's exit code plus a
tamper monitor, never on the model's word.

Bred, not built: every mechanism traces to a named failure across three
generations of its author's harnesses (a lighthouse gauntlet, a swarm,
a house-mind). The scars are the features.

## Get running

```sh
git clone https://github.com/CharlesMod/beekeeper && cd beekeeper && ./install.sh
```

**Lane 1 — cloud API (30 seconds).** Works with any OpenAI-compatible provider:

```sh
bee setup --api          # writes ~/.beekeeper/config
# put your key in that file (or: export BEEKEEPER_API_KEY=sk-...)
bee "fix the failing tests"
```

**Lane 2 — fully local (recommended destination).** Auto-detects your OS,
architecture, and RAM, then downloads an honest tier:

| your RAM | model it picks | download | notes |
|---|---|---|---|
| ≥ 12 GB | Ling-3.0-tiny IQ4_XS (8B MoE) | 4.4 GiB | the reference setup |
| 8–12 GB | same, tight-RAM profile | 4.4 GiB | close heavy apps while it works |
| < 8 GB | LFM2.5-2.6B Q4_K_M | 1.7 GiB | clean tool calling; suits small, single-step tasks |

Honesty note, from our own benchmark: the < 8 GB tier runs the harness
mechanics flawlessly (verified end-to-end), but on a multi-step repair
benchmark it diagnoses without ever committing to an edit. Treat it as a
capable runner of small, concrete tasks — "rename this", "add a flag",
"summarize these logs" — not an autonomous repair agent. For that, use the
≥ 12 GB tier or point `bee` at an API model (Lane 1).

```sh
bee setup                # llama.cpp release binary + tiered model
bee serve                # one terminal (or a login item)
bee "task"               # another
```

Apple Silicon with ≥12 GB can take the fast lane instead — a mixed-precision
quant on an MLX server with prefix caching: `bee setup --mlx`.

## Daily use

```sh
bee "add a --json flag" ~/proj/thing     # task in a named directory
bee -v "make check" "task"               # override the auto-detected verify command
bee -n "task"                            # UNGATED (done becomes the model's word)
bee board                                # waggle: queue tasks from your phone (:8484, tailnet-gated)
```

Exit codes are honest: `0` = done **and verified**, `1` = out of clock without
lying about it, `2` = backend unreachable.

## The doctrine

- The harness moves the model (forced pivots after repeated identical failures).
- The model never settles its own claims (verify gate + tamper monitor).
- The system never lies to the model (every refusal names the real rule).
- Advisory text does not deter; only blocking gates do. An edit that changes
  only numeric literals is refused once — retuning a constant to pass a test
  is the cheat every scaffold converges on. Because constants are sometimes
  genuinely wrong, re-issuing the identical edit overrides the gate: a
  deliberate, named act, on the record.
- The advertised context window is a promise; beekeeper serves only what the
  hardware actually delivers.

MIT. One file each: `beekeeper.py` (the agent), `hatch.py` (setup),
`waggle.py` (the phone board), `bee` (the front door).
