```text
================================================================================
          \ /                                       ( )
        -(|||)@ .  .                            (  )  ( )
                     .  _..--""""--.._  .           ( ) (  )
                  _.-""   .-------.   ""-._          (__)
                 (       /:::::::::\       )          ||
                  '-._  |:::::::::::|  _.-'         .-''-.
                       `|::(o):(o)::|`             |  ~~  |
                        |:::::::::::|              |  ~~  |
                        |:::::--::::|            _ '-....-'
                         \:::::::::/            //       .  .  \ /
                     .----'-.___.-'----.       //               (|||)-@
                    /      |       |     \    //
                   /   /|  |       |  |\  \  //
                  /   / |  |       |  | \  \//
                 (___)  |  |       |  |  (__)
                        |  |  ___  |  |
                        |  | |   | |  |
                        |  | |   | |  |
                       _|__|_|   |_|__|_
                      (______)   (______)


    ___.                    __
    \_ |__    ____    ____  |  | __  ____    ____  ______    ____  _______
     | __ \ _/ __ \ _/ __ \ |  |/ /_/ __ \ _/ __ \ \____ \ _/ __ \ \_  __ \
     | \_\ \  ___/ \  ___/ |    < \  ___/ \  ___/ |  |_> >\  ___/  |  | \/
     |___  / \___  > \___  >|__|_ \ \___  > \___  >|   __/  \___  > |__|
         \/      \/      \/      \/     \/      \/ |__|         \/

       ~  a   t e r s e ,   h o n e s t   c o d i n g   h a r n e s s  ~
                         it cannot lie about being done
================================================================================
```

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
| ≥ 16 GB | Gemma-4-26B-A4B UD-IQ3_S (MoE, 4B active) | 11.3 GB | the reference setup; runs on any GPU from 4 GB up with its experts in system RAM |
| 8 – under 16 GB | Qwen3.5-4B IQ4_XS | 2.5 GB | the default for small tasks; cheapest verified code solve we measured |
| < 8 GB | same, tight-RAM profile | 2.5 GB | close heavy apps while it works |
| optional: `bee setup --architect`, 16 GB+ GPU | Swift-Qwen3.8-27B IQ3_XS (dense 27B reasoning) | 13.0 GB | the "architect" tier; think with reasoning effort low for tool turns, medium for decisions |

<img width="1515" height="1205" alt="interactive chart" src="https://github.com/user-attachments/assets/2815835d-f512-4b9c-b3e8-00556c74fd3b" />


**What runs where:** [an interactive chart](https://htmlpreview.github.io/?https://github.com/CharlesMod/beekeeper/blob/master/docs/what-runs-where.html)
of which models fit which machines (card VRAM, system RAM, context length,
whole on the card or with a MoE's experts in RAM), built from the author's
fleet measurements: pick your card and RAM and see what fits, how much
context, and how fast. The file is [`docs/what-runs-where.html`](docs/what-runs-where.html)
(self-contained; open it in any browser). Its Qwen3.8-27B row stands for Swift,
which has the same architecture and is 0.1 GB smaller.

Every download is checked against its published sha256 before it is used.
Sampler settings for each model are written into `serve.sh`.

Measured in the author's fleet (not by this repo's tests): Qwen3.5-4B solves
a code task for about 21 s each, the cheapest verified solve of the models
tried, and scored 85% on the author's tool-call bench against 65% for the
Ling-3.0-tiny this table used to pick, which lost on measurement. Gemma-4-26B-A4B
decodes about 9.7 tok/s on CPU only, about 11 tok/s on a 4 GB laptop GPU and
about 180 tok/s on a 16 GB card. LFM2.5-2.6B, the old sub-8 GB pick, fails the
probe on the current llama.cpp build and was dropped. Swift-Qwen3.8-27B is a
dense reasoning model tuned against overthinking; its base model's default effort
(xhigh) overthinks badly, so set `BEEKEEPER_THINK=on`,
`BEEKEEPER_EFFORT_EXPLORE=low` and `BEEKEEPER_EFFORT_DECIDE=medium`. Those
two variables send `reasoning_effort` in `chat_template_kwargs` on thinking
turns only, and nothing when unset.

Honesty note: small models are capable runners of small, concrete tasks ("rename
this", "add a flag", "summarize these logs") and weaker as autonomous
multi-step repair agents. For hard repairs use the ≥ 16 GB tier or point `bee`
at an API model (Lane 1).

```sh
bee setup                # llama.cpp release binary + tiered model
bee serve                # one terminal (or a login item)
bee "task"               # another
```

Apple Silicon with ≥12 GB can take the fast lane instead — a mixed-precision
quant on an MLX server with prefix caching: `bee setup --mlx`. (That lane
still serves the older Ling-3.0-tiny MLX quant: no MLX build of the new picks
has been measured yet, so the llama.cpp lane above is the recommended one.)

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

BeeKeeper is the coding bee of a family: the Hive (a self-managing cluster of home machines) and DBee, the Doctor Bee (https://github.com/CharlesMod/dbee), an autonomous on-call doctor for machines.

MIT. One file each: `beekeeper.py` (the agent), `hatch.py` (setup),
`waggle.py` (the phone board), `bee` (the front door).
