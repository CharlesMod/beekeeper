#!/usr/bin/env python3
"""beekeeper — the harness that tends the hive.

Third-generation harness, bred from three of its author's systems:
  keeper (the lighthouse gauntlet) — context discipline, read-dedup, resurrection
  hive (the swarm)   — exit-code capping, tamper monitors, truncation-is-never-an-answer
  eiDOS (the mind)   — parser salvage, normalized action signatures, honesty rule,
                       tree-kill, forced pivots

Doctrine: the harness moves the model; the model never settles its own claims;
the system never lies to the model; a refusal names the real rule.
"""
import argparse, ast, hashlib, json, os, re, shutil, signal, statistics, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error, urllib.parse

def _cfg():
    out = {}
    try:
        for line in open(os.path.expanduser('~/.beekeeper/config')):
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1); out[k.strip()] = v.strip().strip('"')
    except OSError: pass
    return out
_C = _cfg()
def _opt(env, key, default):
    return os.environ.get(env) or _C.get(env) or default
DEF_BASE = _opt('BEEKEEPER_BASE_URL', 'BEEKEEPER_BASE_URL', 'http://127.0.0.1:8008/v1')
DEF_MODEL = _opt('BEEKEEPER_MODEL', 'BEEKEEPER_MODEL', 'local')
API_KEY = _opt('BEEKEEPER_API_KEY', 'BEEKEEPER_API_KEY', '')
# The hive's research organ: a SearXNG-compatible metasearch endpoint.
# Unset means the tool is never offered — the system never advertises a
# capability it does not have.
SEARCH_URL = _opt('BEEKEEPER_SEARCH_URL', 'BEEKEEPER_SEARCH_URL', '')
MAX_TURNS = 60
NUDGE_LIMIT = 3
STALL_LIMIT = 5     # consecutive refused repeats that end the run as stalled (exit 3)
ALT_PERIODS = (2, 3)  # H-33: the cycle lengths the alternation detector reads (period 1 IS the stall law)
ALT_DETOUR = 2      # H-15: unrelated actions between two refusals that do NOT reset the streak
ALT_EDITS = 3       # H-33: successive edits with an unchanged verify outcome before edit is withheld
CREATE_OFFER_TURNS = 3  # H-56: turns a create offer stays in the schema before it expires
RESTART_LIMIT = 4   # H-12: attempts per run at most (the clock is the real budget)
RESTART_FLOOR_S = 60  # never restart into less than this many seconds
EVICT_HEAD = 1200   # H-05: chars of the last verify output's head kept under pressure
EVICT_TAIL = 1200   # ... and of its tail (the summary line lives at the end)

# H-05: the files a task names — the working set's first half. A superset filter
# (a version number reads as a path-ish token); it is matched against basenames
# that were actually read, so a spurious one never matches anything.
_PATHISH = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.[A-Za-z0-9_]{1,5}')


def named_files(text):
    out = set()
    for m in _PATHISH.finditer(text or ''):
        base = m.group(0).replace('\\', '/').rstrip('.').rsplit('/', 1)[-1]
        if '.' in base:
            out.add(base)
    return out
PHASE_K = 6         # H-10: the turn by which the first edit is forced (the ring's winners edit by turn 6)
AUTOVERIFY_MAX_S = 90  # H-32: a verify this slow is not spent unasked (docker verifies cost 20-90 s)
THINK_BUDGET = 512  # base thinking budget per thinking turn (BEEKEEPER_THINK_BUDGET); the server's --reasoning-budget is the ceiling
THINK_CEILING = 4096  # BEEKEEPER_THINK_CEILING: never ask for more thinking than this in one turn
SYMBOLMAP_CHARS = 9000   # H-04: BEEKEEPER_SYMBOLMAP_CHARS — over this a read serves the map
SYMBOLMAP_MAX = 1200     # H-04: the map's own character cap (~1 KB); extras fold into a count
SYMBOLMAP_RANGE_LINES = 400   # H-04: the most lines one ranged read may return
FAIL_KINDS = ('args', 'blocked', 'timeout', 'exec', 'parse', 'network', 'llm')

SYSTEM = """You are beekeeper, a terse repair agent. You fix broken machines (code) with the fewest, most causal moves.

DISCIPLINE — follow strictly:
1. DIAGNOSE FIRST. Run the tests and/or the program to OBSERVE the failure before reading much code.
2. FIX CAUSES, NEVER FIT NUMBERS. Find the wrong OPERATION (a flipped sign, a swapped name, a wrong comparison). Never retune a constant or threshold to make a failing assertion pass.
3. ACT, DON'T NARRATE. Prefer a tool call over prose; at most one short sentence before it.
4. ONE READ EACH. You remember what you read. Re-reading an unchanged file wastes the clock.
5. VERIFY, THEN SELF-CHECK. After edits, re-run the tests. When green, follow the task's self-check instructions EXACTLY — run what it says and READ the output.
6. FINISH. Complete the task's submission step, then call done. done is gated on real verification — it will refuse if the work is not actually done.
7. WHEN THE HARNESS REFUSES, IT NAMES A REAL RULE. Read the refusal; it is never noise."""

# H-07 (M-09): the rules a guard already enforces are deleted, because this
# model class reads the shape of its context and not the list of rules —
# "ONE READ EACH" was violated sixty times in one run while it was in view,
# and the read cache ended it. Each of the seven has code behind it: 1 the
# start verify and the phase gate, 2 the numeric-literal gate, 3 the nudge
# counter and prose salvage, 4 the read cache / collapse / stall law, 5's
# verify half the done gate (and auto-verify), 6's gate the done gate, 7 the
# refusal texts themselves. What survives is what no guard enforces: the
# task's own self-check and submission step. The tools, the verify command
# and the exit-code law stay because they are FACTS about the world, which
# is the one instructional form M-09 keeps.
LEAN = """You are beekeeper, a terse repair agent. You fix broken code.

Tools: {tools}.
Verify: {verify}
When it is green, follow the task's self-check and submission steps exactly \
— run what they say and read the output — then call done. done re-runs the \
verify and refuses unless it exits 0. Exit codes decide, never your summary."""

TOOLS = [
    {"type": "function", "function": {"name": "read", "description": "Read a file (numbered lines; long files show head+tail).",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}}},
    {"type": "function", "function": {"name": "edit", "description": "Replace exact text in a file. old_str must occur exactly once.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"},
                    "old_str": {"type": "string"}, "new_str": {"type": "string"}},
                    "required": ["file_path", "old_str", "new_str"]}}},
    {"type": "function", "function": {"name": "write", "description": "Create or overwrite a file with content.",
     "parameters": {"type": "object", "properties": {"file_path": {"type": "string"},
                    "content": {"type": "string"}}, "required": ["file_path", "content"]}}},
    {"type": "function", "function": {"name": "bash", "description": "Run a shell command in the arena (60s timeout, non-interactive).",
     "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "done", "description": "Finish. Gated: refuses if verification fails or protected files were tampered with.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]
TOOLS.append(
    {"type": "function", "function": {"name": "fetch",
     "description": "Download a URL into the arena. Follows redirects and "
                    "reports what actually arrived (type and size) — use this "
                    "instead of curl for downloads.",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"},
         "save_as": {"type": "string", "description": "filename in the arena"}},
         "required": ["url", "save_as"]}}})
if SEARCH_URL:
    TOOLS.append(
        {"type": "function", "function": {"name": "search",
         "description": "Search the web. Returns ranked title/url/snippet. "
                        "Use this instead of guessing URLs — guessed links 404.",
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string"},
             "limit": {"type": "integer", "description": "max results (default 6)"}},
             "required": ["query"]}}})
REGISTRY = {t['function']['name'] for t in TOOLS}
# H-04: the read schema the model sees when BEEKEEPER_SYMBOLMAP=on. A separate
# constant, never a mutation of TOOLS — a flag-off worker in the same process
# must see the shipped schema unchanged.
RANGED_READ = {"type": "function", "function": {"name": "read",
    "description": ("Read a file (numbered lines). A big file returns its SYMBOL MAP — defs and "
                    "classes with line ranges — not its text; then read the range you need with "
                    "start_line/end_line, which returns exactly those lines."),
    "parameters": {"type": "object", "properties": {
        "file_path": {"type": "string"},
        "start_line": {"type": "integer", "description": "first line to return (1-based, inclusive)"},
        "end_line": {"type": "integer", "description": "last line to return (inclusive)"}},
        "required": ["file_path"]}}}

def system_prompt(policy, verify_cmd=None, tools=None):
    """The system message. `full` (the default) is byte-identical to SYSTEM.
    Under `lean` the tool line is rendered from the tools actually offered —
    pass the turn's own array so the prompt never names a tool the schema
    withholds."""
    if policy != 'lean':
        return SYSTEM
    offered = TOOLS if tools is None else tools
    return LEAN.format(tools=', '.join(t['function']['name'] for t in offered),
                       verify=verify_cmd or '(none — the task names its own check)')


def arena_anchor(policy, arena, task):
    """The arena line keeps the FACT and, under lean, drops the RULE: the
    scope fence blocks (and often corrects) an out-of-arena path already."""
    rule = '' if policy == 'lean' else ' Use relative paths; never leave it.'
    return f"[Arena root: {arena} — your bash commands run there.{rule}]\n\n{task}"

def log(msg): print(msg, flush=True)

def fail(kind, msg):
    assert kind in FAIL_KINDS
    return f"ERROR[{kind}]: {msg}"

# ---------- H-04: the symbol map ----------
# A read of a 306 KB file used to answer "what is in here?" with its first 150
# and last 40 lines — the two places the symbol being hunted is not. The map is
# the file's shape at ~1 KB, and it names the call that fetches the body: a
# range. Structural, not instructional (harness-review.md): the affordance
# changes, no rule is added.
SYM_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?:(?:export|public|private|protected|internal|static|final|abstract|async|pub|def|declare)\s+)*'
    r'(?:'
    r'(?P<kind>class|struct|interface|enum|trait|impl|module|def|func|fn|function|type)\s+'
    r'(?P<name>[A-Za-z_$][\w$]*)'
    r'|func\s+\([^)]*\)\s*(?P<name4>[A-Za-z_][\w]*)'      # go methods
    r'|(?:const|let|var)\s+(?P<name2>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?'
    r'(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)'
    r')')

def _ast_rows(text):
    """(depth, label, start, end) for Python that parses. Nothing else is as
    honest about a class's line range as the compiler's own numbers."""
    tree = ast.parse(text)
    rows, fn = [], (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        end = getattr(node, 'end_lineno', node.lineno) or node.lineno
        if isinstance(node, fn):
            rows.append((0, f"def {node.name}", node.lineno, end))
        elif isinstance(node, ast.ClassDef):
            rows.append((0, f"class {node.name}", node.lineno, end))
            for sub in node.body:
                if isinstance(sub, fn):
                    rows.append((1, f"def {sub.name}", sub.lineno,
                                 getattr(sub, 'end_lineno', sub.lineno) or sub.lineno))
    return rows

def _regex_rows(text):
    """Every other language, and Python the compiler refuses — a file that will
    not parse is exactly when the model most needs to see its shape."""
    lines, hits = text.splitlines(), []
    for i, l in enumerate(lines):
        m = SYM_RE.match(l)
        if not m:
            continue
        name = m.group('name') or m.group('name4') or m.group('name2')
        kind = m.group('kind') or ('func' if m.group('name4') else 'const')
        depth = 0 if not m.group('indent') else 1
        hits.append((depth, f"{kind} {name}", i + 1))
    rows = []
    for n, (depth, label, start) in enumerate(hits):
        nxt = next((s for _, _, s in hits[n + 1:]), len(lines) + 1)
        rows.append((depth, label, start, max(start, nxt - 1)))
    return rows

def symbol_map(path, text, cap=SYMBOLMAP_MAX):
    """The map, or None when the file has no shape worth showing (data, prose:
    a map of nothing is worse than the two ends)."""
    try:
        rows = _ast_rows(text) if path.endswith('.py') else _regex_rows(text)
    except (SyntaxError, ValueError, RecursionError):
        rows = _regex_rows(text)
    if len(rows) < 2:
        return None
    lines = text.splitlines()
    head = (f"{os.path.basename(path)} · {len(lines)} lines · {len(text)} chars · symbol map "
            f"(the body is NOT shown — read what you need with "
            f"read(file_path, start_line=A, end_line=B))")
    render = lambda r: f"{r[2]:>6}-{r[3]:<6} " + ("  " if r[0] else "") + r[1]
    room = cap - len(head) - 40                     # 40 holds the fold note
    # Top level first — the file's outline survives even when its methods cannot.
    # When the outline itself will not fit, it is SAMPLED across the file, never
    # truncated: a map that stops three quarters of the way down is the head-only
    # serve this lever exists to replace.
    cost = lambda idx: len(render(rows[idx])) + 1
    tops = [i for i, r in enumerate(rows) if r[0] == 0]
    chosen = tops
    if sum(cost(i) for i in tops) > room:
        chosen = []
        for k in range(len(tops), 0, -1):
            pick = ([tops[round(i * (len(tops) - 1) / (k - 1))] for i in range(k)]
                    if k > 1 else [tops[0]])
            pick = sorted(set(pick))
            if sum(cost(i) for i in pick) <= room:
                chosen = pick
                break
    used = sum(cost(i) for i in chosen)
    chosen = list(chosen)
    for idx, r in enumerate(rows):        # members in file order fill what is left
        if r[0] != 0:
            if used + cost(idx) > room:
                break
            chosen.append(idx); used += cost(idx)
    keep = sorted(chosen)
    out = [head] + [render(rows[i]) for i in keep]
    if len(keep) < len(rows):
        out.append(f"… +{len(rows) - len(keep)} more symbols not shown")
    return "\n".join(out)

# ---------- H-06: traceback capture ----------
# pytest puts the assertion, the last frames and the FAILED summary at the END
# of its output; bash kept the first 3500 characters. The budget is unchanged —
# what it buys is not.
SALIENT_RE = re.compile(
    r'^(?:E\s|FAILED\b|ERROR\b|[A-Za-z_.]*(?:Error|Exception|Warning)\b.*:|'
    r'\s*Traceback \(most recent call last\)|\s+File "|'
    r'=+ (?:FAILURES|ERRORS|short test summary info)|'
    r'=*\s*\d+ (?:failed|passed|error))')     # pytest's count line: the score itself

def clip_output(text, limit):
    """Keep the head, the tail, and every failure line that would otherwise
    have been elided between them. Output that fits is returned untouched."""
    text = str(text)
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    tail_budget, i, used = max(1, (limit * 3) // 5), len(lines) - 1, 0
    while i >= 0 and used + len(lines[i]) + 1 <= tail_budget:
        used += len(lines[i]) + 1
        i -= 1
    tail_start = max(1, i + 1)
    sal_budget, sal, s_used = max(0, limit // 4), [], 0
    for j in range(tail_start - 1, -1, -1):          # nearest the failure first
        if SALIENT_RE.match(lines[j]):
            if s_used + len(lines[j]) + 1 > sal_budget:
                break
            sal.append(j); s_used += len(lines[j]) + 1
    sal.reverse()
    head_budget, h, h_used = limit - used - s_used - 64, 0, 0
    while h < tail_start and h_used + len(lines[h]) + 1 <= head_budget:
        h_used += len(lines[h]) + 1
        h += 1
    drop = 0                                          # salient lines given up, oldest first
    while True:
        kept = [j for j in sal[drop:] if j >= h]
        marker = (f"... [{tail_start - h - len(kept)} lines elided"
                  + (f"; {len(kept)} failure lines kept" if kept else "") + "] ...")
        out = "\n".join(lines[:h] + [marker] + [lines[j] for j in kept] + lines[tail_start:])
        if len(out) <= limit:
            return out
        if h > 0:                                     # the head yields first
            h -= 1
        elif drop < len(kept):                        # then the oldest rescued frames
            drop += 1
        else:                                         # the tail is last, and never mid-line
            tail_start += 1
            if tail_start >= len(lines):
                return out[-limit:]

def auto_verify(arena):
    """Detect the arena's own check. Verification is the DEFAULT posture."""
    j = lambda *p: os.path.join(arena, *p)
    has_pytests = any(f.startswith('test_') and f.endswith('.py')
                      for f in os.listdir(arena)) or os.path.isdir(j('tests'))
    if has_pytests or os.path.exists(j('pytest.ini')):
        return 'python3 -m pytest -q'
    if os.path.exists(j('package.json')):
        try:
            if 'test' in json.load(open(j('package.json'))).get('scripts', {}):
                return 'npm test --silent'
        except ValueError: pass
    if os.path.exists(j('Makefile')) and 'test:' in open(j('Makefile'), errors='replace').read():
        return 'make test'
    return None

def norm_sig(cmd):
    """One normalized action signature (eiDOS): catches the v3->v4->v5 spiral."""
    s = re.sub(r'["\'`$(){}\[\]\\]', '', str(cmd).lower())
    s = re.sub(r'\d+', '#', s)
    return re.sub(r'\s+', ' ', s).strip()[:120]

def balanced_json(s, start):
    """Brace-balanced, string/escape-aware extractor (eiDOS parser.py lesson)."""
    depth, i, in_str, esc = 0, start, False, False
    while i < len(s):
        c = s[i]
        if esc: esc = False
        elif c == '\\' and in_str: esc = True
        elif c == '"': in_str = not in_str
        elif not in_str:
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: return s[start:i + 1]
        i += 1
    return None

def salvage_tool_calls(text):
    """Gated prose-salvage: line-start `tool {json}` or ```tool\\n{json}``` fences.
    All three guards from eiDOS: line anchor, live registry, parseable balanced JSON."""
    out = []
    for m in re.finditer(r'(?m)^[ \t]{0,4}[`>*-]{0,3}[ \t]*(' + '|'.join(REGISTRY) + r')[ \t]*(\{)', text):
        blob = balanced_json(text, m.start(2))
        if blob:
            try:
                args = json.loads(blob)
                out.append({'id': f'salv_{len(out)}', 'type': 'function',
                            'function': {'name': m.group(1), 'arguments': json.dumps(args)}})
            except ValueError: pass
    for m in re.finditer(r'```(' + '|'.join(REGISTRY) + r')\s*\n\s*(\{)', text):
        blob = balanced_json(text, m.start(2))
        if blob:
            try:
                args = json.loads(blob)
                out.append({'id': f'salv_{len(out)}', 'type': 'function',
                            'function': {'name': m.group(1), 'arguments': json.dumps(args)}})
            except ValueError: pass
    # OpenAI-shaped tool call emitted as content: {"name": T, "arguments": {...}}.
    # Weak tool-callers (7B-class) write the call instead of tagging it; the
    # hive's episode.py recovers this same shape (do not let a real call read
    # as prose). Only fires when the anchored forms above found nothing.
    if not out:
        for m in re.finditer(r'\{', text):
            blob = balanced_json(text, m.start())
            if not blob:
                continue
            try:
                o = json.loads(blob)
            except ValueError:
                continue
            name = o.get('name') if isinstance(o, dict) else None
            if name in REGISTRY and isinstance(o.get('arguments'), (dict, str)):
                args = o['arguments']
                out.append({'id': f'salv_{len(out)}', 'type': 'function',
                            'function': {'name': name,
                                         'arguments': args if isinstance(args, str)
                                         else json.dumps(args)}})
    return out

class Beekeeper:
    ANSWER_ROOM = 700                    # tokens left for the tool call after a think block closes

    def __init__(self, arena, task, verify_cmd=None, base_url=DEF_BASE, model=DEF_MODEL,
                 start_verify=True, net_baseline=None):
        self.arena = os.path.realpath(arena)
        self.url = base_url.rstrip('/').removesuffix('/chat/completions').removesuffix('/v1') + '/v1/chat/completions'
        self.model = model
        self.verify_cmd = verify_cmd
        budget = self._load_budget()
        self.compact_at = int(budget * 0.55) * 4      # chars
        self.hard_limit = int(budget * 0.80) * 4
        log(f"[beekeeper] context budget {budget} tokens ({self.budget_source}); "
            f"compact at {self.compact_at} chars, hard at {self.hard_limit}")
        self._settings_pending = (budget,)
        self.max_tokens = 700
        self.tok_floor = 700                 # raised to 2048 when a thinking model is detected
        self.exhaustions = 0
        self.read_cache = {}
        self.read_msgs = {}                  # message idx -> path, so eviction can un-cache reads
        self.last_full_read = None
        # H-05, the eviction policy (budget-law.md §5). `basic` is the policy as
        # shipped; `working-set` never evicts the files the task names, the last
        # edited file or the last verify output, never un-caches a read, keeps
        # the collapse anchors, and leaves a ledger fact where basic leaves an
        # instruction. The 09-03 ring's sixty identical reads were all of those
        # faults at once, under a budget that was assumed rather than measured.
        self.evict_policy = os.environ.get('BEEKEEPER_EVICT', '').strip().lower() or 'basic'
        self.evict_source = 'env' if os.environ.get('BEEKEEPER_EVICT', '').strip() else 'default'
        self.task_files = named_files(task)   # the working set's first half
        self.last_edit_path = None            # ... and its second: the file last written
        self.last_verify_idx = None           # message index of the most recent verify output
        self.read_evicted = {}                # path -> (lines, turn): where a read's content went
        self.ledger = []
        self.sig_history = []            # (norm_sig, ok) trail for loop pivot
        self.poison = {}                 # norm_sig -> crash count
        self.override_pending = set()    # (path, old_str, new_str) numeric-gate refusals awaiting re-issue
        self.last_result = (None, None)  # (sig, sha) for collapse-with-count
        self.repeat_run = 0
        self.exhausted = {}              # norm_sig -> identical-result count; refused until an edit/write
        self.stall_refusals = 0          # consecutive refused repeats
        self.last_result_idx = None      # index of the surviving tool message for last_result
        self.last_result_text = ''       # its real content (the first, un-collapsed result)
        self.exhausted_idx = {}          # norm_sig -> surviving tool message index
        self.refused_count = {}          # norm_sig -> refusals so far
        self.withheld = set()            # tool names withheld from the schema
        # BEEKEEPER_WITHHOLD = turn (arm D, the keeper's carry 2026-09-05: withheld for
        # the NEXT turn only) | until-execute (arm E: until some action executes)
        self.withhold_policy = os.environ.get('BEEKEEPER_WITHHOLD', '').strip().lower() or 'turn'
        self.withhold_source = 'env' if os.environ.get('BEEKEEPER_WITHHOLD', '').strip() else 'default'
        # Two rungs of one model: BEEKEEPER_THINK = unset (say nothing) | off | on |
        # phase (think on turn 1 and after a red verify; never after an edit or a
        # green verify). The choice is logged per turn — measured, not asserted.
        # unset is OFF, sent explicitly: under a pin that leaves thinking to the
        # harness, saying nothing would let the server's default decide in silence
        self.think_policy = os.environ.get('BEEKEEPER_THINK', '').strip().lower() or 'off'
        self.think_source = 'env' if os.environ.get('BEEKEEPER_THINK', '').strip() else 'default'
        self.temperature = float(os.environ.get('BEEKEEPER_TEMPERATURE') or 0)
        self.temperature_source = 'env' if os.environ.get('BEEKEEPER_TEMPERATURE') else 'default'
        self.bash_timeout = int(os.environ.get('BEEKEEPER_BASH_TIMEOUT') or 180)
        # H-09, the regression net at done: the visible tests are named; the
        # tests that decide are their siblings in the same files (pool v2's
        # iceberg: three named cases greened, four siblings left red, done).
        self.net_policy = os.environ.get('BEEKEEPER_NET', '').strip().lower() or 'on'
        self.net_source = 'env' if os.environ.get('BEEKEEPER_NET', '').strip() else 'default'
        self.net_cmd = self._net_cmd(verify_cmd) if (verify_cmd and self.net_policy != 'off') else None
        # H-09b: under `bg` the baseline runs in a thread over a COPY of the tree
        # taken before turn 1 — a run that edits pays nothing on the clock for a
        # gate that fired in 0 of 28 runs on pool v2, and H-09's law (the
        # baseline precedes the first edit) is kept by waiting, not by hurrying
        self.net_snapshot = None         # the copy's root, outside the arena; removed at the end
        self.net_thread = None
        self.net_bg = None               # the thread's failing set; None if it could not run
        # H-32, the auto-verify: after every successful edit or write the harness
        # runs the verify itself and the result is the next observation — progress
        # becomes visible without the model asking. Off by default.
        self.autoverify_policy = os.environ.get('BEEKEEPER_AUTOVERIFY', '').strip().lower() or 'off'
        self.autoverify_source = 'env' if os.environ.get('BEEKEEPER_AUTOVERIFY', '').strip() else 'default'
        self.autoverify_max_s = float(os.environ.get('BEEKEEPER_AUTOVERIFY_MAX_S') or AUTOVERIFY_MAX_S)
        self.autoverifies = 0            # M-04: opportunity counts, reported whether the lever fired or not
        self.autoverify_skipped = 0
        self.autoverify_turn = None      # the turn the decision was last taken (at most one a turn)
        self.autoverify_notes = set()    # skip reasons already said once (a repeated note is an attractor)
        self.last_verify_wall = None     # what the harness's own verify costs, measured not assumed
        self.board_policy = os.environ.get('BEEKEEPER_BOARD', '').strip().lower() or 'off'
        self.board_source = 'env' if os.environ.get('BEEKEEPER_BOARD', '').strip() else 'default'
        self.board_rows = {}            # test id -> (state, turn): red | green | '?'; flipped by verification alone
        self.board_flips = []           # (turn, id) each time a row turns green
        if self.board_policy == 'on' and verify_cmd:
            for m in self._TEST_ID.finditer(verify_cmd):
                self.board_rows.setdefault(f"{m.group(3)}::{m.group(4)}", ('?', 0))
        # H-02, the trend: the checkpoint law already scores every verify
        # (`failing_count`) and that scalar died in the ledger — the model saw a
        # fresh red wall each turn and could not tell a fix that moved two
        # tests from one that moved none. The board's head carries the
        # DIRECTION: `red 2/2 → red 1/2 improving`. Off by default.
        self.trend_policy = os.environ.get('BEEKEEPER_TREND', '').strip().lower() or 'off'
        self.trend_source = 'env' if os.environ.get('BEEKEEPER_TREND', '').strip() else 'default'
        self.trend_history = []          # (turn, failing count) per MEASURED verify
        self.trend_prev = None           # the count before the latest observation
        self.trend_now = None            # ...and after it
        self.trend_total = None          # the denominator: the visible set
        self.trend_dir = None            # improving | regressing | flat
        # H-08, the ladder: H-01 seeds its rows from the ids in the verify
        # COMMAND, so a verify that names a FILE (the public ring's shape)
        # renders no board at all, and a run that greens one of four tests
        # learns only that a count fell. The rungs come from pytest's own
        # per-test lines instead, fenced to the files the verify names, and the
        # budget is spent on the failing rungs first. The rungs ARE board rows,
        # so the ladder writes only where H-01 gave it a board. Off by default.
        self.ladder_policy = os.environ.get('BEEKEEPER_LADDER', '').strip().lower() or 'off'
        self.ladder_source = 'env' if os.environ.get('BEEKEEPER_LADDER', '').strip() else 'default'
        self.ladder_discovered = 0       # M-04: rungs the OUTPUT added to the board
        self._verify_file_set = None     # the fence, parsed once from the verify command
        self.restart_policy = os.environ.get('BEEKEEPER_RESTART', '').strip().lower() or 'off'
        self.restart_source = 'env' if os.environ.get('BEEKEEPER_RESTART', '').strip() else 'default'
        # H-10, the phase law: the SCHEMA carries the phase, so orientation and
        # commitment are affordances rather than instructions (92 of 125 unsolved
        # arm-runs never edited a file, in every harness; the winners edit by
        # turn 6, and a rule the model must obey is text it will not follow).
        # Turn one carries no edit/write; both enter once a verify has been
        # OBSERVED (the start verify counts); a red verify that FOLLOWS an edit
        # takes edit out until a read or bash executes (anchoring on one region
        # after red is the failure); and turn k with no edit yet takes read and
        # bash out, so the only actions left are edit, write and done.
        self.phase_policy = os.environ.get('BEEKEEPER_PHASE', '').strip().lower() or 'off'
        self.phase_source = 'env' if os.environ.get('BEEKEEPER_PHASE', '').strip() else 'default'
        self.phase_k = int(os.environ.get('BEEKEEPER_PHASE_K') or PHASE_K)
        self.phase_k_source = 'env' if os.environ.get('BEEKEEPER_PHASE_K') else 'default'
        self.phase_withheld = set()      # names the phase keeps out of THIS turn's schema
        self.phase_state = None          # (withheld, reason) of the last recorded transition
        self.phase_forcing = False       # the first-edit forcing is in effect this turn
        self.phase_edit_blocked = False  # a red verify followed an edit; edit is out
        # a gate on an observation that cannot exist would deadlock the run: with
        # no verify at all there is nothing to observe, so only turn one is gated
        self.phase_verified = (verify_cmd is None) or bool(start_verify)
        # the opportunity counts M-04 needs, measured in EVERY arm (a lever whose
        # gate was never reached is unmeasured, not inert) — the levers themselves
        # act only under the flag
        self.first_edit_turn = None      # the turn the first edit or write LANDED
        self.forced_edit = False         # ...and it landed while read and bash were withheld
        self.forced_turns = 0            # turns the forcing was in effect
        self.red_after_edit = 0          # red verifies that followed an edit: the gate's opportunities
        self.edit_withheld_after_red = 0 # ...of which this many took edit out of the schema
        self.edits_since_verify = 0
        # H-33 / H-15: the stall law is a PERIOD-1 detector — it compares a
        # result with the one immediately before it — so two actions taken in
        # turns never trip it (pool v2's remaining stalls), and a single
        # unrelated action between two refusals resets the streak to zero
        # forever. BEEKEEPER_ALT=on extends the SAME counters (stall_refusals,
        # exhausted, refused_count) to periods 2 and 3, to a detour of one or
        # two actions, and to edits that leave the verify exactly where it was.
        self.alt_policy = os.environ.get('BEEKEEPER_ALT', '').strip().lower() or 'off'
        self.alt_source = 'env' if os.environ.get('BEEKEEPER_ALT', '').strip() else 'default'
        self.alt_edits = int(os.environ.get('BEEKEEPER_ALT_EDITS') or ALT_EDITS)
        self.alt_trail = []              # (sig, sterile) per action; progress clears it
        self.sig_result = {}             # sig -> the result sha it last returned
        self.alt_detours = 0             # unrelated actions since the last refusal
        self.alt_note = None             # one line owed to the model, flushed after the turn's calls
        self.alt_stall = False           # why the streak hit the limit with no refusal to carry the exit
        self.flat_edits = 0              # successful edits since the verify outcome last moved
        self.alt_flat = False            # MEASURED: the last edits moved the verify nowhere
        self.last_outcome = None         # (failing count, failing names) of the last verify observed
        self.alternations = 0            # M-04 opportunity counts: reported whether the flag is on or off
        self.no_progress_events = 0
        self.wanders = 0
        # H-56, missing file -> create: an output that names an import of a path
        # this arena does not have puts a `create` tool for exactly that path in
        # the NEXT turn's schema — an affordance, not a sentence. Off by default;
        # with it off nothing is scanned and the run is byte-identical.
        # H-07: `lean` deletes every rule a guard enforces; `full` (default) is
        # today's prompt, byte for byte. An unknown word is full — the prompt
        # is never a variant nobody named.
        _rules = os.environ.get('BEEKEEPER_RULES', '').strip().lower()
        self.rules_policy = _rules if _rules in ('full', 'lean') else 'full'
        self.rules_source = 'env' if _rules in ('full', 'lean') else 'default'
        self.create_policy = os.environ.get('BEEKEEPER_CREATE', '').strip().lower() or 'off'
        self.create_source = 'env' if os.environ.get('BEEKEEPER_CREATE', '').strip() else 'default'
        self.create_offer = None         # {turn, targets, paths, taken, noted}: one live offer
        self.create_offers = 0           # H-51: the gate — missing paths seen
        self.create_taken = 0            # H-51: the act — files created through the offer
        # H-04, the symbol map: over the threshold a read serves the file's SHAPE
        # (defs, classes, line ranges) and the schema grows start_line/end_line, so
        # the next call fetches the body it actually wants. Off by default: with it
        # off the read tool's schema, its output and its refusals are unchanged.
        self.symbolmap_policy = os.environ.get('BEEKEEPER_SYMBOLMAP', '').strip().lower() or 'off'
        self.symbolmap_source = 'env' if os.environ.get('BEEKEEPER_SYMBOLMAP', '').strip() else 'default'
        self.symbolmap_chars = int(os.environ.get('BEEKEEPER_SYMBOLMAP_CHARS') or SYMBOLMAP_CHARS)
        self.symbolmap_chars_source = 'env' if os.environ.get('BEEKEEPER_SYMBOLMAP_CHARS') else 'default'
        self.mapped = set()              # paths whose last serve was a map, not a body
        self.maps_served = 0             # H-51 opportunity counts: big reads answered with a map
        self.ranged_reads = 0            # …and ranges the model then asked for
        # H-06, traceback capture: the same character budget buys head AND tail,
        # with the pytest failure lines lifted out of the elided middle. Off by
        # default: with it off every truncation is byte-identical.
        self.traceback_policy = os.environ.get('BEEKEEPER_TRACEBACK', '').strip().lower() or 'off'
        self.traceback_source = 'env' if os.environ.get('BEEKEEPER_TRACEBACK', '').strip() else 'default'
        self.clips = 0                   # H-51: outputs that were too big to keep whole
        # H-51 (M-04): every lever's OPPORTUNITY count, kept where the lever acts
        self.attempt = 1                 # run_attempts overwrites it; restarts = attempt - 1
        self.exhaustion_events = 0       # actions the stall law exhausted
        self.withheld_turns = 0          # turns whose schema was short a tool
        self.net_baselines = 0           # baselines the net actually measured
        self.net_gate_reached = 0        # times done reached the net's judgment
        self.net_refusals = 0            # times the net refused a done
        self.end_reason = None
        self.net_baseline = None           # names failing in those files before the first edit
        self.net_override = False          # a second done accepts pre-existing siblings
        self.bash_timeout_source = 'env' if os.environ.get('BEEKEEPER_BASH_TIMEOUT') else 'default'
        self.turn = 0
        self.last_verify_red = False     # the most recent verify came back red
        self.after_verify = False        # the previous action was a verify
        self.think_log = []              # (turn, think) per turn
        # The smarter budget (llama.cpp's --reasoning-budget is server-wide, so the
        # harness spends it per turn through max_tokens): base x phase, capped by
        # the ceiling and a quarter of the clock, doubled after an overflow, halved
        # back toward the base after an early close. An overflow continues the turn
        # once with thinking off and the partial thought quoted — a thought becomes
        # an action, never a wasted turn.
        self.think_base = int(os.environ.get('BEEKEEPER_THINK_BUDGET') or THINK_BUDGET)
        self.think_ceiling = int(os.environ.get('BEEKEEPER_THINK_CEILING') or THINK_CEILING)
        self.think_next = self.think_base   # the adaptive term
        self.tok_rate = 60.0              # tokens/s, refined from measured thinking turns
        self.edits_since_green = 0
        self.force_no_think = False       # set for a continuation request
        self.current_budget = 0
        self.max_seconds = None
        self.t0 = time.time()
        # The spend ledger (budget-law.md §7): one JSONL record per turn and one
        # at the end, beside the transcripts (BEEKEEPER_SPEND_DIR), never in the
        # arena. Every parameter of the budget law is chosen from it.
        self.spend_path = None
        d = os.environ.get('BEEKEEPER_SPEND_DIR', '').strip()
        if d:
            os.makedirs(d, exist_ok=True)
            # unique per run: inside a container every worker is PID 1 and four
            # start in the same second (pool v2: 25 ledgers for 28 runs)
            self.spend_path = os.path.join(d, f"{int(time.time())}-{os.getpid()}-"
                                              f"{hashlib.sha1(self.arena.encode()).hexdigest()[:8]}.jsonl")
        self.spend_turn = {}                 # the record being assembled this turn
        self.last_think = (0, 0, None)       # (budget, used, closed) of the latest thinking turn
        self.compactions = 0
        self.pin_idx = set()             # message indices compaction must never evict
        anchored = arena_anchor(self.rules_policy, self.arena, task)
        self.messages = [{"role": "system", "content": system_prompt(self.rules_policy, verify_cmd)},
                         {"role": "user", "content": anchored}]
        self.pin_idx.update({0, 1})
        self.snapshot = self._tree_hash()
        self.protected = self._protected_set()
        self.assert_base = self._assert_count()
        if net_baseline is not None:
            self.net_baseline = set(net_baseline)   # a restart carries the baseline measured before the FIRST edit
        if verify_cmd and start_verify:
            code, out0 = self._run_verify()
            self._observe(code, out0, turn=0)
            self.last_outcome = self._verify_outcome(out0)   # the tree as it stands: no-progress reads from here
            self._scan_missing(out0, 0)
            if code == 0:
                log("[beekeeper] WARNING: verify already green at start — a check that cannot fail cannot gate")
        if self.net_policy == 'bg' and self.net_cmd and self.net_baseline is None:
            self._net_bg_start()
        log(f"[beekeeper] settings: budget={budget}({self.budget_source}) think={self.think_policy}({self.think_source}) "
            f"think_budget={self.think_base} think_ceiling={self.think_ceiling} "
            f"temperature={self.temperature:g}({self.temperature_source}) "
            f"bash_timeout={self.bash_timeout}({self.bash_timeout_source}) "
            f"withhold={self.withhold_policy}({self.withhold_source}) net={self.net_policy}({self.net_source}) board={self.board_policy}({self.board_source}) restart={self.restart_policy}({self.restart_source}) "
            f"trend={self.trend_policy}({self.trend_source}) ladder={self.ladder_policy}({self.ladder_source}) "
            f"phase={self.phase_policy}({self.phase_source}) phase_k={self.phase_k}({self.phase_k_source}) "
            f"alt={self.alt_policy}({self.alt_source}) alt_edits={self.alt_edits} "
            f"autoverify={self.autoverify_policy}({self.autoverify_source}) autoverify_max_s={self.autoverify_max_s:g} "
            f"evict={self.evict_policy}({self.evict_source}) "
            f"withhold={self.withhold_policy}({self.withhold_source}) net={self.net_policy}({self.net_source}) board={self.board_policy}({self.board_source}) restart={self.restart_policy}({self.restart_source}) create={self.create_policy}({self.create_source}) max_turns={MAX_TURNS} "
            f"rules={self.rules_policy}({self.rules_source}) "
            f"symbolmap={self.symbolmap_policy}({self.symbolmap_source}) symbolmap_chars={self.symbolmap_chars}({self.symbolmap_chars_source}) "
            f"traceback={self.traceback_policy}({self.traceback_source}) "
            f"nudge_limit={NUDGE_LIMIT} stall_limit={STALL_LIMIT} answer_room={self.ANSWER_ROOM} "
            f"max_tokens={self.max_tokens} model={self.model}")

    # ---------- tamper monitors (hive quorum) ----------
    def _tree_hash(self):
        out = {}
        for root, _, files in os.walk(self.arena):
            for f in files:
                p = os.path.join(root, f)
                try: out[os.path.relpath(p, self.arena)] = hashlib.sha1(open(p, 'rb').read()).hexdigest()
                except OSError: pass
        return out

    def _changed_files(self):
        """Files whose bytes differ from the start-of-episode snapshot."""
        now = self._tree_hash()
        return {p for p in set(now) | set(self.snapshot) if now.get(p) != self.snapshot.get(p)}

    def _protected_set(self):
        pats = ('test_', 'conftest', 'check_')
        return {p for p in self.snapshot if os.path.basename(p).startswith(pats)}

    def _assert_count(self):
        n = 0
        for p in self.protected:
            try: n += open(os.path.join(self.arena, p), errors='replace').read().count('assert')
            except OSError: pass
        return n

    def tamper_violations(self):
        v = []
        now = self._tree_hash()
        for p in self.protected:
            if now.get(p) != self.snapshot.get(p):
                v.append(f"protected file modified: {p}")
        if self._assert_count() < self.assert_base:
            v.append(f"assertion count dropped ({self.assert_base} -> {self._assert_count()})")
        return v

    # ---------- budget ----------
    def _load_budget(self):
        """The context budget, measured before assumed. Resolution order: the
        operator's word (BEEKEEPER_CONTEXT_TOKENS — a bench pins it), the
        machine's standing config (~/.beekeeper.json), the server's own
        /props (llama.cpp reports the per-slot n_ctx), then the old default.
        The 09-03 public ring assumed 24,000 while the server offered
        131,072: compaction fired every turn at a tenth of the room, evicted
        the working file, un-cached it, reset the repeat counter and told the
        model to re-run the tool — sixty identical reads. Sets budget_source."""
        v = os.environ.get('BEEKEEPER_CONTEXT_TOKENS', '').strip()
        if v.isdigit() and int(v) > 0:
            self.budget_source = 'env'; return int(v)
        try:
            n = int(json.load(open(os.path.expanduser('~/.beekeeper.json')))['context_budget_tokens'])
            if n > 0:
                self.budget_source = 'file'; return n
        except Exception:
            pass
        try:
            root = self.url[:-len('/v1/chat/completions')]
            req = urllib.request.Request(root + '/props', headers={'User-Agent': 'beekeeper/1.0'})
            with urllib.request.urlopen(req, timeout=2) as r:
                props = json.loads(r.read())
            n = int((props.get('default_generation_settings') or {}).get('n_ctx') or props.get('n_ctx') or 0)
            if n > 0:
                self.budget_source = 'server'; return n
        except Exception:
            pass
        self.budget_source = 'default'; return 24000

    # ---------- tools ----------
    def _inside(self, p):
        rp = os.path.realpath(p if os.path.isabs(p) else os.path.join(self.arena, p))
        if rp == self.arena or rp.startswith(self.arena + os.sep):
            return rp, None
        # small models echo their location imperfectly; a unique basename match is graced, with a note
        base = os.path.basename(rp)
        hits = [os.path.join(r, base) for r, _, fs in os.walk(self.arena) if base in fs]
        if len(hits) == 1:
            rel = os.path.relpath(hits[0], self.arena)
            return hits[0], f"[path corrected to {rel} — the path you gave pointed outside the arena] "
        return None, None

    def t_read(self, file_path, start_line=None, end_line=None):
        ranged = start_line is not None or end_line is not None
        if ranged and self.symbolmap_policy != 'on':
            return fail('args', "read takes only file_path")
        p, note = self._inside(file_path)
        if not p: return fail('blocked', f"{file_path} is outside the arena and nothing in it matches that filename")
        try: body = open(p, errors='replace').read()
        except OSError as e: return fail('args', str(e))
        lines = body.splitlines()
        if ranged:
            # A range is a DIFFERENT view of the file, so the unchanged-cache never
            # blocks it: having been shown the map, the model must be able to fetch
            # what the map named. The repeat is the stall law's business, not this
            # tool's — an identical range thrice is exhausted like any other action.
            try:
                a = int(start_line if start_line is not None else 1)
                b = int(end_line if end_line is not None else a)
            except (TypeError, ValueError):
                return fail('args', "start_line and end_line must be whole numbers")
            if a < 1 or b < a:
                return fail('args', f"bad range {a}-{b}: 1 <= start_line <= end_line")
            if a > len(lines):
                return fail('args', f"{file_path} has {len(lines)} lines; start_line {a} is past the end")
            capped = min(b, len(lines), a + SYMBOLMAP_RANGE_LINES - 1)
            self.last_full_read = p          # eviction un-caches this path like any read
            self.ranged_reads += 1
            out = '\n'.join(f"{i}|{lines[i - 1]}" for i in range(a, capped + 1))
            if capped < min(b, len(lines)):
                out += (f"\n... [stopped at line {capped}: one read returns at most "
                        f"{SYMBOLMAP_RANGE_LINES} lines — ask for the next range]")
            return (note or '') + out
        sha = hashlib.sha1(body.encode()).hexdigest()
        self.last_full_read = None
        if self.read_cache.get(p) == sha:
            # H-05: a read whose result was evicted is still served FROM THE
            # CACHE — never re-sent whole, which is what un-caching used to
            # force. The reply says where the content went; it gives no order.
            if self.evict_policy == 'working-set' and p in self.read_evicted:
                lines, turn = self.read_evicted[p]
                return (f"[unchanged since your last read — read {os.path.relpath(p, self.arena)}, "
                        f"{lines} lines, evicted at turn {turn}]")
            if self.symbolmap_policy == 'on' and p in self.mapped:
                return ("[unchanged since your last read — what you have is this file's symbol map, "
                        "not its body. Read the lines you need: "
                        "read(file_path, start_line=A, end_line=B).]")
            return "[unchanged since your last read — you already have this file in context]"
        self.read_cache[p] = sha
        self.last_full_read = p
        if self.symbolmap_policy == 'on' and len(body) > self.symbolmap_chars:
            m = symbol_map(p, body, SYMBOLMAP_MAX)
            if m:                            # a file with no shape keeps head+tail
                self.mapped.add(p)
                self.maps_served += 1
                return m
        self.mapped.discard(p)
        if len(body) > 9000:
            shown = lines[:150] + [f"... [{len(lines) - 190} lines omitted — use edit for targeted changes] ..."] + lines[-40:]
        else:
            shown = [f"{i+1}|{l}" for i, l in enumerate(lines)]
        return '\n'.join(shown)

    @staticmethod
    def _freshen(p, prev_mtime):
        """A rewrite that keeps byte size and lands in the same second leaves
        Python's __pycache__ trusting stale bytecode — verify would then test
        the OLD code and lie red (or green). Nudge mtime past the record."""
        st = os.stat(p)
        if int(st.st_mtime) <= int(prev_mtime):
            os.utime(p, (st.st_atime, int(prev_mtime) + 1))

    def _syntax_guard(self, p, before):
        if not p.endswith('.py'): return None
        try:
            compile(open(p, errors='replace').read(), p, 'exec')
            return None
        except SyntaxError as e:
            open(p, 'wb').write(before)
            return fail('parse', f"edit produced a syntax error (line {e.lineno}: {e.msg}) — rolled back")

    def _edit_grace(self, s, old_str, new_str):
        """Small models paraphrase whitespace and copy the N| display prefixes from read
        output. If the content still identifies a unique block, grace the edit with a note."""
        o = re.sub(r'(?m)^\s*\d+\|', '', old_str)
        w = re.sub(r'(?m)^\s*\d+\|', '', new_str)
        if o != old_str and s.count(o) == 1:
            return o, w, "[line-number prefixes stripped — the N| in read output is display, not file content] "
        flines, olines = s.split('\n'), o.split('\n')
        tgt = [l.strip() for l in olines]
        if not any(tgt): return None, None, None
        hits = [i for i in range(len(flines) - len(olines) + 1)
                if [l.strip() for l in flines[i:i + len(olines)]] == tgt]
        if len(hits) != 1: return None, None, None
        i = hits[0]
        exact = '\n'.join(flines[i:i + len(olines)])
        if s.count(exact) != 1: return None, None, None
        shift = (len(flines[i]) - len(flines[i].lstrip())) - (len(olines[0]) - len(olines[0].lstrip()))
        nlines = w.split('\n')
        if shift > 0:
            nlines = [(' ' * shift + l if l.strip() else l) for l in nlines]
        elif shift < 0:
            nlines = [l[min(-shift, len(l) - len(l.lstrip())):] if l.strip() else l for l in nlines]
        return exact, '\n'.join(nlines), "[old_str matched by content with corrected whitespace] "

    def t_edit(self, file_path, old_str, new_str):
        p, note = self._inside(file_path)
        if not p: return fail('blocked', f"{file_path} is outside the arena and nothing in it matches that filename")
        try: s = open(p).read()
        except OSError as e: return fail('args', str(e))
        n = s.count(old_str)
        if n == 0:
            g_old, g_new, g_note = self._edit_grace(s, old_str, new_str)
            if g_old is not None:
                old_str, new_str, note = g_old, g_new, (note or '') + g_note
                n = s.count(old_str)
        if n == 0: return fail('args', "old_str not found — copy the exact lines from a fresh read, "
                                       "without the N| line-number prefixes")
        if n > 1: return fail('args', f"old_str occurs {n} times; add context to make it unique")
        # anti-Goodhart gate: an edit that ONLY changes numbers is usually tuning, not fixing.
        # Refused once; re-issuing the identical edit applies it (constants ARE sometimes wrong).
        numeric_only = old_str != new_str and re.sub(r'[\d.]+', '#', old_str) == re.sub(r'[\d.]+', '#', new_str)
        override_key = (p, old_str, new_str)
        if numeric_only and override_key not in self.override_pending:
            self.override_pending.add(override_key)
            return fail('blocked', "this edit changes ONLY numeric literals. Retuning a constant to "
                                   "satisfy a test is the wrong fix — find the wrong OPERATION "
                                   "(sign, comparison, name), not the constant. If the constant itself "
                                   "is genuinely wrong, re-issue this exact edit now to override this "
                                   "gate — a deliberate, named act, on the record.")
        self.override_pending.discard(override_key)
        self._net_baseline()
        before = s.encode()
        prev_mtime = os.stat(p).st_mtime
        open(p, 'w').write(s.replace(old_str, new_str))
        err = self._syntax_guard(p, before)
        if err: return err
        self._freshen(p, prev_mtime)
        self.read_cache.pop(p, None)
        self.read_evicted.pop(p, None)
        self.last_edit_path = p          # H-05: the working set's second half
        tag = " [numeric-only, applied on override]" if numeric_only else ""
        self.ledger.append(f"edit {os.path.basename(p)}: {old_str.strip()[:50]!r} -> {new_str.strip()[:50]!r}{tag}")
        if numeric_only:
            return ("OK: replaced 1 occurrence. NOTE: this edit changed only numeric literals and was "
                    "applied on your override. If you are tuning a constant to satisfy a test, that is "
                    "the wrong fix — find the wrong OPERATION (sign, comparison, name) instead.")
        return (note or '') + "OK: replaced 1 occurrence"

    def t_write(self, file_path, content):
        self._net_baseline()
        p, note = self._inside(file_path)
        if not p: return fail('blocked', f"{file_path} is outside the arena and nothing in it matches that filename")
        if os.path.exists(p):
            old = open(p, errors='replace').read()
            if old.count('\n') >= 40 and len(content) < len(old) * 0.5:
                return fail('blocked', "refusing whole-file write that shrinks a large file by >50% — "
                                       "the model tends to emit only the fragment it reasoned about; use edit")
            before = old.encode()
            prev_mtime = os.stat(p).st_mtime
        else:
            before, prev_mtime = None, None
        open(p, 'w').write(content)
        if before is not None:
            err = self._syntax_guard(p, before)
            if err: return err
            self._freshen(p, prev_mtime)
        self.read_cache.pop(p, None)
        self.read_evicted.pop(p, None)
        self.last_edit_path = p          # H-05 (create routes through write too)
        self.ledger.append(f"write {os.path.basename(p)} ({len(content)} chars)")
        return (note or '') + f"OK: wrote {len(content)} chars"

    def t_create(self, file_path, content):
        """H-56: a `write` restricted to the paths the harness derived from a
        real import failure. Any other path is refused — the offer names what
        is missing, and naming it is the whole point."""
        if not self._create_live():
            return fail('args', "unknown tool create")
        paths = self.create_offer["paths"]
        p, _ = self._inside(file_path)
        rel = os.path.relpath(p, self.arena) if p else str(file_path)
        if rel not in paths:
            return fail('blocked', "create writes only the missing paths this harness named: "
                                   + ", ".join(paths) + f" — {file_path} is not one of them. "
                                   "Use write for any other file.")
        d = os.path.dirname(os.path.join(self.arena, rel))
        if d:
            os.makedirs(d, exist_ok=True)
        out = self.t_write(rel, content)
        if str(out).startswith('ERROR'):
            return out
        if self.ledger and self.ledger[-1].startswith('write '):
            self.ledger[-1] = f"create {rel} ({len(content)} chars)"
        self.create_taken += 1
        self.create_offer["taken"] = True
        return f"OK: created {rel} — {len(content)} chars"

    # ---------- H-56: the paths an output says are missing ----------
    # A missing NAME in a module that exists is deliberately its own regex: the
    # module is there, the symbol is not, and that is an edit, never a create.
    _MOD_RE = re.compile(r"""No module named ['"]?([A-Za-z_][\w.]*)""")
    _NAME_RE = re.compile(r"""cannot import name ['"]([A-Za-z_]\w*)['"] from ['"]?([A-Za-z_][\w.]*)""")
    _FNF_RE = re.compile(r"""FileNotFoundError[^\n]*?['"]([^'"\n]+)['"]""")

    @staticmethod
    def _module_paths(dotted):
        rel = dotted.replace('.', '/')
        return [rel + '.py', rel + '/__init__.py']

    def _present(self, rel):
        return os.path.exists(os.path.join(self.arena, rel))

    def _imported_by_a_test(self, name):
        pat = re.compile(r'(?m)^\s*(?:from|import)\s+' + re.escape(name) + r'\b')
        n = 0
        for root, dirs, files in os.walk(self.arena):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', '.venv', 'node_modules')]
            for f in files:
                if not f.endswith('.py') or not (f.startswith('test_') or f.endswith('_test.py')
                                                 or os.path.basename(root) == 'tests'):
                    continue
                n += 1
                if n > 400:
                    return False
                try:
                    if pat.search(open(os.path.join(root, f), errors='replace').read()):
                        return True
                except OSError:
                    pass
        return False

    def _declared_dependency(self, head):
        key = head.replace('_', '-').lower()
        for f in ('requirements.txt', 'requirements-dev.txt', 'pyproject.toml',
                  'setup.py', 'setup.cfg', 'Pipfile'):
            p = os.path.join(self.arena, f)
            if not os.path.exists(p):
                continue
            try:
                body = open(p, errors='replace').read().replace('_', '-').lower()
            except OSError:
                continue
            if re.search(r"""(?m)(^|[\s"'\[=,>])""" + re.escape(key) + r"""($|[\s"'\]=<>~!,;])""", body):
                return True
        return False

    def _create_rooted(self, dotted):
        """A candidate is the arena's to write only when the import is ROOTED
        here: the top package already exists in the tree, or a bare name is
        imported by one of the arena's own tests and named by no dependency
        manifest. A missing third-party package is a dependency, not a file to
        invent — writing `numpy.py` into the tree would shadow the real fix."""
        head = dotted.split('.')[0]
        if os.path.isdir(os.path.join(self.arena, head)) or self._present(head + '.py'):
            return True
        if '.' in dotted:
            return False
        return self._imported_by_a_test(head) and not self._declared_dependency(head)

    def _file_candidate(self, raw):
        raw = str(raw).strip()
        if not raw or raw.startswith('~'):
            return None
        q = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(self.arena, raw))
        if not q.startswith(self.arena + os.sep) or os.path.exists(q):
            return None
        rel = os.path.relpath(q, self.arena)
        if not os.path.splitext(rel)[1] and os.sep not in rel:
            return None      # a bare token is a program that is not installed, not a file to write
        return rel

    def _scan_missing(self, out, turn):
        """Read a verify's or a bash call's output for imports of paths that
        are not in this arena and open ONE offer over the candidates."""
        if self.create_policy != 'on' or not out:
            return
        text = str(out)
        targets, seen = [], set()

        def add(group):
            # a module's two candidates are ALTERNATIVES: if either is already
            # in the tree the module is present and the fault is inside it
            if not group or any(self._present(p) for p in group):
                return
            key = tuple(group)
            if key not in seen:
                seen.add(key)
                targets.append(group)

        for name, module in self._NAME_RE.findall(text):
            if any(self._present(p) for p in self._module_paths(module)):
                # the module IS there; the symbol is not. Recorded, never offered.
                self._spend({"kind": "create_skip", "turn": turn, "reason": "name-not-path",
                             "module": module, "name": name})
                continue
            if self._create_rooted(module):
                add(self._module_paths(module))
        for module in self._MOD_RE.findall(text):
            if self._create_rooted(module):
                add(self._module_paths(module))
        for raw in self._FNF_RE.findall(text):
            c = self._file_candidate(raw)
            if c:
                add([c])
        if not targets:
            return
        self._close_create_offer()          # one live offer at a time
        paths = [p for g in targets for p in g]
        self.create_offer = {"turn": turn, "targets": targets, "paths": paths,
                             "taken": False, "noted": False}
        self.create_offers += 1
        log(f"[beekeeper] create offered (t{turn}): {', '.join(paths)}")

    def _create_live(self):
        o = self.create_offer
        return (self.create_policy == 'on' and bool(o)
                and (self.turn - o["turn"]) <= CREATE_OFFER_TURNS)

    def _close_create_offer(self):
        o = self.create_offer
        if not o:
            return
        self.create_offer = None
        self._spend({"kind": "create", "turn": o["turn"], "offered": o["paths"], "taken": o["taken"]})

    def _offer_create(self, turn):
        """The offer lives until the path exists or three turns pass; the note
        naming the candidates is written once, when the offer first reaches a
        schema."""
        o = self.create_offer
        if self.create_policy != 'on' or not o:
            return
        if all(any(self._present(p) for p in g) for g in o["targets"]) or turn - o["turn"] > CREATE_OFFER_TURNS:
            self._close_create_offer()
            return
        if not o["noted"]:
            o["noted"] = True
            self.messages.append({"role": "user", "content":
                "[create offered: the last output names " + ", ".join(o["paths"])
                + " — paths this arena does not have. The `create` tool writes exactly one of "
                  "them and refuses any other path.]"})

    def _create_tool(self):
        paths = list(self.create_offer["paths"])
        return {"type": "function", "function": {
            "name": "create",
            "description": ("Create a file the last output showed as imported but ABSENT from this "
                            "arena: " + ", ".join(paths) + ". Only those paths; any other is refused "
                            "(use write or edit for files that already exist)."),
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "enum": paths},
                "content": {"type": "string"}},
                "required": ["file_path", "content"]}}}

    def t_bash(self, command):
        sig = norm_sig(command)
        if self.poison.get(sig, 0) >= 2:
            return fail('blocked', "this command has crashed/timed out twice and is quarantined — "
                                   "change approach entirely")
        try:
            p = subprocess.Popen(command, shell=True, cwd=self.arena, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                out, _ = p.communicate(timeout=self.bash_timeout)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError: p.kill()
                out, _ = p.communicate()
                self.poison[sig] = self.poison.get(sig, 0) + 1
                return fail('timeout', f"command timed out at {self.bash_timeout}s (process tree killed). "
                                       "Non-interactive commands only; everything must exit on its own.")
        except OSError as e:
            return fail('exec', str(e))
        if p.returncode < 0 or p.returncode >= 126:
            self.poison[sig] = self.poison.get(sig, 0) + 1
        out = (out or '').strip() or "(no output)"
        if len(out) > 3500:
            # H-06: the same budget, spent on both ends. The head-only cut threw
            # away the assertion, the last frames and the FAILED summary — the
            # only part of a pytest run that says what to do next.
            if self.traceback_policy == 'on':
                self.clips += 1
                out = clip_output(out, 3500)
            else:
                out = out[:3500] + "\n... [truncated]"
        self.ledger.append(f"bash: {command[:60]} -> exit {p.returncode}")
        return f"exit {p.returncode}\n{out}"

    def t_fetch(self, url, save_as):
        """Research needs hands: a leaf that can pull a file down stops
        fighting curl flags. Reports what ARRIVED, not what was asked
        for — an HTML error page saved as .mp3 is the classic silent
        failure, so it is named instead."""
        p, note = self._inside(save_as)
        if not p:
            p = os.path.join(self.arena, os.path.basename(str(save_as)))
        if not str(url).lower().startswith(('http://', 'https://')):
            return fail('args', "url must be http(s)")
        try:
            req = urllib.request.Request(str(url), headers={
                'User-Agent': 'Mozilla/5.0 (compatible; beekeeper/1.0)'})
            with urllib.request.urlopen(req, timeout=120) as r:
                ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip()
                size = 0
                h = hashlib.sha1()
                with open(p, 'wb') as out:
                    while True:
                        chunk = r.read(65536)
                        if not chunk: break
                        out.write(chunk); size += len(chunk); h.update(chunk)
        except urllib.error.HTTPError as e:
            return fail('network', f"HTTP {e.code} — that URL is not there; search for a real one")
        except Exception as e:
            return fail('network', f"{type(e).__name__}: {str(e)[:120]}")
        self.read_cache.pop(p, None)
        rel = os.path.relpath(p, self.arena)
        if size < 1024 or ctype.startswith(('text/html', 'application/xhtml')):
            head = open(p, 'rb').read(200)
            os.unlink(p)
            return fail('network',
                        f"that URL returned {ctype or 'unknown'} ({size} bytes), not a file — "
                        f"it looks like a web page, not the asset. Starts: {head[:80]!r}")
        self.ledger.append(f"fetch {rel} ({size} bytes, {ctype})")
        return f"OK: saved {rel} — {size} bytes, content-type {ctype or 'unknown'}"

    def t_search(self, query, limit=6):
        """Research is a first-class capability, not a prompt paragraph:
        a leaf that can search stops guessing URLs and starts finding
        them (hive service, keeper direction 2026-08-25)."""
        if not SEARCH_URL:
            return fail('blocked', "this bench has no search service configured")
        url = (SEARCH_URL.rstrip('/') + '/search?format=json&q='
               + urllib.parse.quote(str(query)))
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'beekeeper/1.0'})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            return fail('network', f"search failed: {type(e).__name__}: {str(e)[:120]}")
        try:
            n = max(1, min(12, int(limit)))
        except (TypeError, ValueError):
            n = 6
        lines = []
        for i, r in enumerate(data.get('results', [])[:n], 1):
            snippet = re.sub(r'\s+', ' ', str(r.get('content') or ''))[:180]
            lines.append(f"{i}. {r.get('title','')}\n   {r.get('url','')}\n   {snippet}")
        return '\n'.join(lines) or "(no results — try different terms)"

    # an id is a file, `::`, a name; the name stops at whitespace, quotes and
    # shell punctuation (a bare id is followed by the `;` that ends pytest)
    _TEST_ID = re.compile(r"""(\s*)(['"]?)([\w./-]+\.py)::([^\s'";&|()<>]+)\2""")

    @classmethod
    def _net_cmd(cls, verify_cmd):
        """The verify over the FILES the named tests live in, one entry per
        file (a dropped duplicate takes its leading whitespace with it); None
        when the command names no tests."""
        seen = []
        def sub(m):
            f = m.group(3)
            if f in seen:
                return ''
            seen.append(f)
            return m.group(1) + f
        out = cls._TEST_ID.sub(sub, verify_cmd)
        return out if seen else None

    @staticmethod
    def failing_tests(output):
        """Names pytest reports as FAILED or ERROR in its short summary."""
        return set(re.findall(r'^(?:FAILED|ERROR) (\S+\.py::\S+?)(?: - .*)?$', output or '', re.M))

    def _run_cmd(self, cmd, cwd=None):
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd or self.arena, capture_output=True, text=True,
                               errors='replace', timeout=self.bash_timeout)
            return r.returncode, (r.stdout or '') + (r.stderr or '')
        except subprocess.TimeoutExpired:
            return 124, f"(net timed out at {self.bash_timeout}s)"

    def _observe(self, code, out, turn):
        """A verify observation flips board rows: exit 0 greens every row;
        otherwise the ids pytest names as FAILED/ERROR go red and the rest
        of the named set — which ran and did not fail — go green. A run
        that could not collect (exit 4/5, "no tests ran") measures nothing."""
        text = str(out or '')
        # M-02: a red exit is not evidence that tests ran, and the trend and
        # the ladder are as bound by that as the board is
        if code in (4, 5) or 'no tests ran' in text or 'not found:' in text:
            return
        if self.trend_policy == 'on':
            self._observe_trend(code, text, turn)
        # H-08: the rungs may be discovered by this very observation, so the
        # ladder speaks before the row loop reads self.board_rows
        said = self._observe_rungs(text) if (self.ladder_policy == 'on'
                                             and self.board_policy == 'on') else {}
        if not self.board_rows:
            return
        failing = self.failing_tests(text) if code != 0 else set()
        for tid, (state, _) in list(self.board_rows.items()):
            new = said.get(tid) or ('red' if tid in failing else 'green')
            if new == 'green' and state != 'green':
                self.board_flips.append((turn, tid))
            self.board_rows[tid] = (new, turn)

    # ---------- H-02, the trend ----------
    def _observe_trend(self, code, text, turn):
        """The failing count of a MEASURED verify, and the direction it moved.
        A green run with no summary is zero failing; an opaque red proves
        nothing about progress and leaves the trend exactly where it was."""
        n = self.failing_count(text)
        if n is None and code == 0:
            n = 0
        if n is None:
            return
        self.trend_prev, self.trend_now = self.trend_now, n
        self.trend_total = len(self.board_rows) or self._ran_total(text)
        self.trend_dir = self._trend_dir(self.trend_prev, n)
        self.trend_history.append((turn, n))
        if self.spend_turn:           # the start verify runs before any turn record exists
            self.spend_turn["trend"] = self.trend_record()

    @staticmethod
    def _trend_dir(prev, now):
        if prev is None:
            return None               # a first observation has no predecessor to move from
        return 'improving' if now < prev else ('regressing' if now > prev else 'flat')

    @staticmethod
    def _ran_total(text):
        """How many tests the run measured, from pytest's own summary — the
        denominator when no board row set gives one. None when it says."""
        parts = re.findall(r'(\d+) (failed|passed|errors?)\b', text or '')
        return sum(int(n) for n, _ in parts) or None

    def trend_record(self):
        return {"prev": self.trend_prev, "now": self.trend_now,
                "total": self.trend_total, "dir": self.trend_dir}

    def _trend_seg(self):
        """` · trend red 2/2 → red 1/2 improving`, in the units the model is
        scored in. Empty while the lever is off or nothing has been measured;
        no arrow and no direction word on the first observation of a run."""
        if self.trend_policy != 'on' or self.trend_now is None:
            return ''
        def part(n):
            return f"{'green' if n == 0 else 'red'} {n}" + (f"/{self.trend_total}" if self.trend_total else "")
        if self.trend_prev is None:
            return f" · trend {part(self.trend_now)}"
        return f" · trend {part(self.trend_prev)} → {part(self.trend_now)} {self.trend_dir}"

    # ---------- H-08, the ladder ----------
    _RUNG_SUMMARY = re.compile(r'^(FAILED|ERROR|PASSED|XPASS|XFAIL)\s+([\w./-]+\.py::\S+?)(?: - .*)?$', re.M)
    _RUNG_VERBOSE = re.compile(r'^([\w./-]+\.py::\S+?)\s+(FAILED|ERROR|PASSED|SKIPPED|XPASS|XFAIL)\b', re.M)
    _PY_PATH = re.compile(r'[\w./-]+\.py')

    def _verify_files(self):
        """The files the verify command names: the ladder's fence. A command
        that names no file at all fences nothing — there is nothing to fence
        it to, and an unbounded rung is better than no board."""
        if self._verify_file_set is None:
            self._verify_file_set = {os.path.normpath(p)
                                     for p in self._PY_PATH.findall(self.verify_cmd or '')}
        return self._verify_file_set

    def _observe_rungs(self, text):
        """What the verify's own per-test lines said, id by id — the short
        summary form (`FAILED tests/t.py::a - boom`) and the verbose form
        (`tests/t.py::a FAILED [ 33%]`) alike. Ids inside the fenced files
        that the board does not carry yet become new rungs."""
        said = {}
        for m in self._RUNG_SUMMARY.finditer(text):
            said[m.group(2)] = 'red' if m.group(1) in ('FAILED', 'ERROR') else 'green'
        for m in self._RUNG_VERBOSE.finditer(text):
            said.setdefault(m.group(1), 'red' if m.group(2) in ('FAILED', 'ERROR') else 'green')
        named = self._verify_files()
        bases = {os.path.basename(p) for p in named}
        def inside(tid):
            f = tid.split('::')[0]
            return (not named) or os.path.normpath(f) in named or os.path.basename(f) in bases
        said = {t: s for t, s in said.items() if inside(t)}
        for tid in said:
            if tid not in self.board_rows:
                self.board_rows[tid] = ('?', 0)
                self.ladder_discovered += 1
        return said

    def _render_board(self, turn):
        """One message, rebuilt in place every turn, pinned: the visible tests
        as the court last measured them, the clock, and the edits so far."""
        if not self.board_rows:
            return
        mark = {'red': '✗', 'green': '✓', '?': '?'}
        left = ''
        if self.max_seconds:
            left = f" · {max(0, int(self.max_seconds - (time.time() - self.t0)))}s left"
        edited = sorted(self._changed_files())[:4]
        head = (f"[Board t{turn}{left} · edits {len(edited)}"
                + (f" ({', '.join(edited)})" if edited else "") + self._trend_seg() + "]")
        rows = []
        items = list(self.board_rows.items())
        if self.ladder_policy == 'on':
            # H-08: the budget is spent on the rungs that still fail — a stable
            # sort, so within a state the rows keep the order they arrived in
            items.sort(key=lambda kv: {'red': 0, '?': 1, 'green': 2}.get(kv[1][0], 3))
        for tid, (state, seen) in items:
            rows.append(f"{mark[state]} {tid} — {state}" + (f" (t{seen})" if seen else " (start)"))
        shown, rest = rows[:12], rows[12:]
        body = "\n".join([head] + shown + ([f"… +{len(rest)} more rows"] if rest else []))[:1200]
        idx = next((i for i, m in enumerate(self.messages)
                    if m.get('role') == 'user' and str(m.get('content', '')).startswith('[Board')), None)
        if idx is None:
            self.messages.insert(2, {"role": "user", "content": body})
            self.pin_idx = {i + 1 if i >= 2 else i for i in self.pin_idx} | {2}
            self.read_msgs = {i + 1 if i >= 2 else i: p for i, p in self.read_msgs.items()}
            if getattr(self, 'last_result_idx', None) is not None and self.last_result_idx >= 2:
                self.last_result_idx += 1
            if self.last_verify_idx is not None and self.last_verify_idx >= 2:
                self.last_verify_idx += 1
            self.exhausted_idx = {k: (v + 1 if v >= 2 else v) for k, v in self.exhausted_idx.items()}
        else:
            self.messages[idx]["content"] = body
            self.pin_idx.add(idx)

    def _net_bg_start(self):
        """H-09b: the baseline on a start-of-run snapshot, in a thread, so the
        model reads while it runs. The COPY is synchronous — a snapshot taken
        while the model edits is not a snapshot — and it lives outside the
        arena, which the tamper monitor walks. A copy that cannot be made is
        named and the run falls back to the synchronous baseline."""
        t = time.time()
        try:
            root = tempfile.mkdtemp(prefix='bk-net-')
            snap = os.path.join(root, 'arena')
            shutil.copytree(self.arena, snap, symlinks=True, ignore_dangling_symlinks=True)
        except (OSError, shutil.Error) as e:
            log(f"[beekeeper] net bg: snapshot failed ({e}) — the baseline will be measured synchronously")
            return
        self.net_snapshot = root
        wall = time.time() - t
        self._spend({"kind": "net", "stage": "snapshot", "wall": round(wall, 2), "path": snap})

        def work():
            try:
                _, out = self._run_cmd(self.net_cmd, cwd=snap)
                self.net_bg = self.failing_tests(out)
            except Exception as e:          # the snapshot is removed under it when a run ends early
                log(f"[beekeeper] net bg: baseline did not finish ({type(e).__name__}: {e})")
        self.net_thread = threading.Thread(target=work, daemon=True)
        self.net_thread.start()
        log(f"[beekeeper] net baseline running in the background on a {wall:.1f}s snapshot")

    def _net_baseline(self):
        """Measured once, before the first edit or write: which tests in the
        named tests' files fail as the tree stands. Never assumed.

        Under `bg` the measurement is already running on the start-of-run
        snapshot: an edit that arrives first WAITS for it — H-09's law is that
        the baseline precedes the first edit — and the wait is a record."""
        if self.net_cmd is None or self.net_baseline is not None:
            return
        if self.net_thread is not None:
            alive = self.net_thread.is_alive()
            t = time.time()
            self.net_thread.join()
            waited = time.time() - t
            self.net_thread = None
            if alive:
                log(f"[beekeeper] net baseline: waited {waited:.1f}s for the background baseline")
                self._spend({"kind": "net", "stage": "wait", "waited": round(waited, 2)})
            if self.net_bg is not None:
                self.net_baseline = self.net_bg
                log(f"[beekeeper] net baseline: {len(self.net_baseline)} failing in the named tests' files"
                    + (" (background)" if not alive else f" (background, {waited:.1f}s waited)"))
                self._spend({"kind": "net", "stage": "baseline", "failing": len(self.net_baseline),
                             "names": sorted(self.net_baseline)[:40], "bg": True,
                             "waited": round(waited, 2)})
                return
            log("[beekeeper] net bg: no background result — measuring synchronously")
        code, out = self._run_cmd(self.net_cmd)
        self.net_baseline = self.failing_tests(out)
        self.net_baselines += 1
        log(f"[beekeeper] net baseline: {len(self.net_baseline)} failing in the named tests' files"
            + (": " + ", ".join(sorted(self.net_baseline))[:300] if self.net_baseline else ""))
        self._spend({"kind": "net", "stage": "baseline", "failing": len(self.net_baseline),
                     "names": sorted(self.net_baseline)[:40]})

    # ---------- H-33 / H-15: alternation, no progress, the wander ----------
    def _verify_outcome(self, output):
        """What a verify MEASURED: the failing count and the failing names.
        None when it measured nothing (an opaque or uncollected run proves
        nothing about progress — M-02: unmeasured is not red)."""
        n = self.failing_count(output)
        return None if n is None else (n, frozenset(self.failing_tests(output)))

    def _alt_cycle(self):
        """The smallest period in ALT_PERIODS whose last two repetitions are
        every one of them sterile — each member of the cycle has now returned
        its identical result three times, which is the period-1 law's own
        threshold, read over a cycle instead of over one action. 0 = none."""
        for p in ALT_PERIODS:
            tail = self.alt_trail[-2 * p:]
            sigs = [s for s, _ in tail]
            if (len(tail) == 2 * p and all(sterile for _, sterile in tail)
                    and sigs[:p] == sigs[p:] and len(set(sigs)) >= 2):
                return p
        return 0

    def _alt_step(self, sig, sterile, progress, turn):
        """One action on the trail. Progress clears it (the world moved, and
        progress is never punished). Otherwise a closed cycle counts as ONE
        refusal streak and exhausts its members, so the next call in the
        pattern is refused before it runs and the pair ends the run stalled
        instead of wandering to the cap."""
        if progress:
            self.alt_trail.clear()
            return
        self.alt_trail.append((sig, sterile))
        p = self._alt_cycle()
        if not p:
            return
        cycle = [s for s, _ in self.alt_trail[-p:]]
        self.stall_refusals += 1
        self.alternations += 1
        for s in cycle:
            self.exhausted[s] = max(self.exhausted.get(s, 0),
                                    sum(1 for t, _ in self.alt_trail if t == s))
        self._spend({"kind": "alt", "turn": turn, "pattern": "aba", "period": p,
                     "sigs": cycle, "streak": self.stall_refusals})
        log(f"[beekeeper t{turn}] alternation: {' / '.join(cycle)} taken in turns with unchanged "
            f"results — one streak ({self.stall_refusals}/{STALL_LIMIT}), both exhausted")

    def _alt_no_progress(self, output, turn):
        """H-33's second half: edits that leave the verify exactly where it was
        are not progress. N of them (BEEKEEPER_ALT_EDITS) withhold `edit` for
        one turn and say so in one line; a recurrence joins the stall streak."""
        outcome = self._verify_outcome(output)
        if outcome is None:
            return False
        prev, self.last_outcome = self.last_outcome, outcome
        if prev is None or outcome != prev:
            self.flat_edits, self.alt_flat = 0, False   # the verify moved: edits are progress again
            return False
        if self.flat_edits < self.alt_edits:
            return False
        if self.no_progress_events:
            self.stall_refusals += 1     # the first withholds; a recurrence counts toward the stall
        self.no_progress_events += 1
        n, names = outcome
        self._spend({"kind": "alt", "turn": turn, "pattern": "no_progress", "edits": self.flat_edits,
                     "failing": n, "tests": sorted(names)[:20], "withheld": "edit",
                     "streak": self.stall_refusals})
        self.alt_note = (f"[no progress: {self.flat_edits} edits and the verify has not moved "
                         f"({n} failing" + (f": {', '.join(sorted(names))[:200]}" if names else "") +
                         "). edit is withheld for one turn — re-read the failing output and find the "
                         "wrong operation before editing again.]")
        self.withheld.add('edit')
        self.flat_edits, self.alt_flat = 0, True
        return True

    def _run_verify(self):
        """The gate's own check. Its wall is recorded: what the harness may
        spend unasked (H-32) is decided from what the last one actually cost,
        never from an assumption."""
        t = time.time()
        try:
            r = subprocess.run(self.verify_cmd, shell=True, cwd=self.arena, capture_output=True,
                               text=True, timeout=120, stdin=subprocess.DEVNULL)
            whole = r.stdout + r.stderr
            # H-06: the verify's mirror fault — the tail alone drops the command,
            # the collection line and the first error that explains the rest.
            if self.traceback_policy == 'on':
                if len(whole) > 1500:
                    self.clips += 1
                out = clip_output(whole, 1500)
            else:
                out = whole[-1500:]
            code = r.returncode
        except subprocess.TimeoutExpired:
            code, out = 124, "verify timed out"
        self.last_verify_wall = time.time() - t
        return code, out

    def _autoverify(self, turn):
        """H-32: the harness verifies after an edit and the result is the next
        observation — the model sees what its edit did without having asked.

        Budgeted, because a verify is not free: at most one decision a turn
        (and so never twice without an edit between, since only a successful
        edit or write calls this), never when the last verify cost more than
        the budget, never when the clock cannot afford another one. A skip is
        one line, said once per reason — a note repeated every turn is the
        attractor the stall law exists to break.

        Downstream, it IS a verify: the board, `last_verify_red`,
        `edits_since_green`, the phase and the turn's spend record all read it
        exactly as they read one the model ran. The stall law is the one law
        that must NOT see it: the harness's observation is not the model's
        action, so it takes no signature, exhausts nothing and withholds
        nothing."""
        if self.autoverify_policy != 'on' or not self.verify_cmd or self.autoverify_turn == turn:
            return
        self.autoverify_turn = turn
        why = None
        if self.last_verify_wall is not None and self.last_verify_wall > self.autoverify_max_s:
            why = (f"the last verify took {self.last_verify_wall:.0f}s, over the "
                   f"{self.autoverify_max_s:g}s budget")
        elif self.max_seconds is not None and (self.max_seconds - (time.time() - self.t0)) < self.autoverify_max_s:
            why = f"under {self.autoverify_max_s:g}s left on the clock"
        if why:
            self.autoverify_skipped += 1
            log(f"[beekeeper t{turn}] auto-verify skipped: {why}")
            if why not in self.autoverify_notes:
                self.autoverify_notes.add(why)
                self.messages.append({"role": "user", "content":
                                      f"[auto-verify skipped — {why}; the harness did not run the verify this turn.]"})
            return
        code, out = self._run_verify()
        wall = self.last_verify_wall or 0.0
        failing = self.failing_count(out)
        self.autoverifies += 1
        self._observe(code, out, turn)
        self.after_verify = True
        self.last_verify_red = code != 0
        if not self.last_verify_red:
            self.edits_since_green = 0
        self.spend_turn["verify"] = 'red' if self.last_verify_red else 'green'
        self.spend_turn["failing"] = failing
        self._spend({"kind": "autoverify", "turn": turn, "code": code,
                     "failing": failing, "wall": round(wall, 2)})
        head = f"exit {code}" + (f" · {failing} failing" if failing is not None else "")
        body = (f"[auto-verify — the harness ran the verify command itself after your edit; "
                f"you did not ask for this and it did not cost you a turn]\n{head}\n"
                + (clip_output(out, 1200) if self.traceback_policy == 'on' else str(out)[-1200:]))
        idx = len(self.messages)
        self.messages.append({"role": "tool", "tool_call_id": f"autoverify-t{turn}", "content": body})
        self._pin_red(idx, body)
        log(f"[beekeeper t{turn}] auto-verify: {head} ({wall:.1f}s)")

    def t_done(self, summary):
        v = self.tamper_violations()
        if v:
            return fail('blocked', "done refused — tamper monitor: " + "; ".join(v) +
                                   ". Restore the protected files; the specification is not yours to edit.")
        if self.verify_cmd:
            code, out = self._run_verify()
            self._observe(code, out, self.turn)
            self._scan_missing(out, self.turn)
            if code != 0:
                return fail('blocked', f"done refused — verify exited {code}. The work is not done:\n{out}")
        if self.net_cmd:
            self.net_gate_reached += 1
            self._net_baseline()
            ncode, nout = self._run_cmd(self.net_cmd)
            now = self.failing_tests(nout)
            regressions = sorted(now - (self.net_baseline or set()))
            siblings = sorted(now & (self.net_baseline or set()))
            self._spend({"kind": "net", "stage": "done", "failing": len(now), "regressions": regressions[:40],
                         "siblings": siblings[:40], "override": self.net_override})
            if regressions:
                self.net_refusals += 1
                return fail('blocked', "done refused — regression: tests in the files your tests live in "
                                       f"passed before your edits and fail now: {', '.join(regressions)[:400]}\n"
                                       f"{nout[-1200:]}")
            if siblings and not self.net_override:
                self.net_override = True
                self.net_refusals += 1
                return fail('blocked', "done refused ONCE — the named tests pass, but tests in the same files "
                                       f"still fail ({len(siblings)}): {', '.join(siblings)[:400]}. They failed "
                                       "before you started, so they may be part of this issue. Fix them if "
                                       "they are yours; re-issue done to accept them as pre-existing.\n"
                                       f"{nout[-1200:]}")
        return None  # signals acceptance

    # ---------- context discipline ----------
    def _size(self):
        return sum(len(str(m.get('content') or '')) + len(str(m.get('tool_calls') or '')) + 200
                   for m in self.messages)

    def compact(self, hard=False):
        """H-05. Two policies, chosen by BEEKEEPER_EVICT; `basic` is the one
        that shipped. Either way the compaction is recorded in the spend
        ledger — naming what left the context and what stayed — so that the
        control arm is readable too (M-04: a lever whose gate was never
        reached is unmeasured, not inert)."""
        self.compactions += 1
        before = self._size()
        if self.evict_policy == 'working-set':
            evicted, kept = self._compact_working_set(hard)
        else:
            evicted, kept = self._compact_basic(hard)
        self._spend({"kind": "compact", "turn": self.turn, "hard": bool(hard),
                     "policy": self.evict_policy, "evicted": evicted[:40], "kept": kept[:40],
                     "n_evicted": len(evicted), "n_kept": len(kept), "compactions": self.compactions,
                     "chars_before": before, "chars_after": self._size()})

    def _compact_basic(self, hard):
        keep = 4 if hard else 8
        n = len(self.messages)
        evictable = [i for i in range(2, n - keep) if i not in self.pin_idx]
        if not evictable: return [], []
        evicted, kept = [], []
        for i in evictable:
            m = self.messages[i]
            if m.get('role') == 'tool' and len(str(m.get('content') or '')) > 200:
                evicted.append(self._msg_label(i))
                m['content'] = "(evicted to fit context — re-run the tool if needed)"
                # evicting a read result means the model no longer has that file:
                # un-cache it so the re-run this notice asks for actually serves content
                if i in self.read_msgs:
                    self.read_cache.pop(self.read_msgs.pop(i), None)
        if evicted:
            self.last_result, self.repeat_run = (None, None), 0
        ledger = '\n'.join(self.ledger[-30:]) or '(none yet)'
        self.messages.insert(2, {"role": "user", "content":
            f"[context compacted: {len(evicted)} old tool results elided. Action ledger:\n{ledger}\n"
            f"Do not re-read unchanged files.]"})
        # the shipped policy shifts only these two: the collapse anchors going
        # stale here is part of what `working-set` fixes, and the control does
        # not get the fix
        self.pin_idx = {i + 1 if i >= 2 else i for i in self.pin_idx}
        self.read_msgs = {i + 1 if i >= 2 else i: p for i, p in self.read_msgs.items()}
        log(f"[beekeeper] compacted ({'hard' if hard else 'soft'}): {len(evicted)} results elided")
        return evicted, kept

    def _working_set(self):
        """The files this run is actually working on: the ones the task names
        and the one last edited. Basenames, because the model writes relative
        paths and the harness stores absolute ones."""
        work = set(self.task_files)
        if self.last_edit_path:
            work.add(os.path.basename(self.last_edit_path))
        return work

    def _msg_label(self, i):
        p = self.read_msgs.get(i)
        if p:
            return f"read {os.path.relpath(p, self.arena)}"
        if i == self.last_verify_idx:
            return "verify"
        return f"tool#{i}"

    def _shift_indices(self):
        """One message was inserted at index 2: every index the harness holds
        into self.messages moves with it. last_result_idx and exhausted_idx
        are the collapse machinery's anchors — a compaction that forgot them
        rewrote the wrong message on the next repeat."""
        bump = lambda i: i + 1 if i >= 2 else i
        self.pin_idx = {bump(i) for i in self.pin_idx}
        self.read_msgs = {bump(i): p for i, p in self.read_msgs.items()}
        self.exhausted_idx = {k: bump(v) for k, v in self.exhausted_idx.items()}
        if self.last_result_idx is not None:
            self.last_result_idx = bump(self.last_result_idx)
        if self.last_verify_idx is not None:
            self.last_verify_idx = bump(self.last_verify_idx)

    @staticmethod
    def _head_tail(s, head=EVICT_HEAD, tail=EVICT_TAIL):
        if len(s) <= head + tail:
            return s
        return f"{s[:head]}\n[... {len(s) - head - tail} chars elided ...]\n{s[-tail:]}"

    def _compact_working_set(self, hard):
        """H-05, budget-law.md §5. The working set — the files the task names,
        the last edited file, the last verify output's head and tail — is never
        evicted; nothing is un-cached; the placeholder states a ledger fact and
        gives no instruction; the repeat counter and the collapse anchors are
        untouched; and the notice is ONE message rebuilt in place (I7: a budget
        message is never a pattern the model completes). The 09-03 ring's sixty
        identical reads were the opposite of every clause here."""
        keep = 4 if hard else 8
        n = len(self.messages)
        safe = set(self.pin_idx)
        for i in (self.last_verify_idx, self.last_result_idx):
            if i is not None:
                safe.add(i)
        safe |= {i for i in self.exhausted_idx.values() if i is not None}
        work = self._working_set()
        evicted, kept = [], []
        for i in range(2, n - keep):
            m = self.messages[i]
            body = str(m.get('content') or '')
            if m.get('role') != 'tool' or len(body) <= 200:
                continue
            path = self.read_msgs.get(i)
            if i in safe or (path and os.path.basename(path) in work):
                if i == self.last_verify_idx:
                    m['content'] = self._head_tail(body)
                kept.append(self._msg_label(i))
                continue
            evicted.append(self._msg_label(i))
            lines = body.count('\n') + 1
            if path:
                rel = os.path.relpath(path, self.arena)
                m['content'] = f"[read {rel}, {lines} lines, evicted at turn {self.turn}]"
                # the cache is NOT dropped: a re-read is served from it, never
                # re-sent whole. What is remembered is where the content went.
                self.read_evicted[path] = (lines, self.turn)
                del self.read_msgs[i]
            else:
                m['content'] = f"[{len(body)} chars of tool output, evicted at turn {self.turn}]"
        ledger = '\n'.join(self.ledger[-30:]) or '(none yet)'
        body = (f"[context compacted {self.compactions}x; {len(evicted)} tool results elided, "
                f"{len(kept)} kept. Action ledger:\n{ledger}]")
        idx = next((i for i, m in enumerate(self.messages)
                    if m.get('role') == 'user'
                    and str(m.get('content') or '').startswith('[context compacted')), None)
        if idx is None:
            self.messages.insert(2, {"role": "user", "content": body})
            self._shift_indices()
            self.pin_idx.add(2)
        else:
            self.messages[idx]['content'] = body
            self.pin_idx.add(idx)
        log(f"[beekeeper] compacted ({'hard' if hard else 'soft'}, working-set): "
            f"{len(evicted)} results elided, {len(kept)} kept")
        return evicted, kept

    def _pin_red(self, idx, content):
        """Never evict the most recent failing verify/test output (hive NEVER_TRIM)."""
        low = content[:500].lower()
        if ('failed' in low or 'error' in low) and ('test' in low or 'exit 1' in low or 'exit 2' in low):
            self.pin_idx = {i for i in self.pin_idx if i in (0, 1)} | {idx}

    # ---------- model ----------
    def tools(self):
        """The schema the next turn sees. After an action is exhausted, the
        tool that looped is withheld — an affordance removed, not a note
        appended (the ctx128k-stall arm, 2026-09-04: told 'refused, take a
        different action', the model re-issued the identical read five
        times) — and every further refusal withholds its tool too, until
        some action actually executes (arm D: two exhausted actions
        alternating, each withheld one turn, stalled across the pair).
        done always stays."""
        withheld = (self.withheld | self.phase_withheld) - {'done'}
        if self.withhold_policy == 'turn':
            self.withheld = set()        # consumed by this build: one turn only (arm D)
        if withheld:
            self.withheld_turns += 1
        base = (TOOLS + [self._create_tool()]) if self._create_live() else TOOLS
        if self.symbolmap_policy == 'on':      # H-04: a new list; TOOLS is never mutated
            base = [RANGED_READ if t["function"]["name"] == 'read' else t for t in base]
        if not withheld:
            return base
        return [t for t in base if t["function"]["name"] not in withheld]

    def _phase_gate(self, turn):
        """H-10. Decide, once per turn, which tools the phase keeps out of the
        schema, and record every transition. The forcing yields when edit and
        write are themselves withheld: a schema of `done` alone is not a phase,
        it is a wall."""
        if self.phase_policy != 'on':
            return
        w, reasons = set(), []
        if turn == 1:
            w |= {'edit', 'write'}; reasons.append('turn_one')
        elif not self.phase_verified:
            w |= {'edit', 'write'}; reasons.append('no_verify_observed')
        if self.phase_edit_blocked:
            w.add('edit'); reasons.append('edit_after_red')
        forcing = (turn >= self.phase_k and self.first_edit_turn is None
                   and not {'edit', 'write'} <= (w | (self.withheld - {'done'})))
        if forcing:
            w |= {'read', 'bash'}; reasons.append('force_first_edit')
            self.forced_turns += 1
        self.phase_forcing, self.phase_withheld = forcing, w
        reason = '+'.join(reasons) or 'open'
        if (frozenset(w), reason) == self.phase_state:
            return
        self.phase_state = (frozenset(w), reason)
        out = (w | self.withheld) - {'done'}
        self._spend({"kind": "phase", "turn": turn, "reason": reason,
                     "schema": [t['function']['name'] for t in TOOLS
                                if t['function']['name'] not in out]})
        if forcing:
            self.messages.append({"role": "user", "content":
                f"[t{turn}: no edit yet — read and bash are out of the schema until you edit or write.]"})

    def _spend(self, rec):
        if not self.spend_path:
            return
        rec = dict(rec, arena=self.arena)      # every record attributable on its own
        with open(self.spend_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, separators=(',', ':')) + "\n")

    def phase(self):
        if self.turn == 1:
            return 'turn_one'
        if self.after_verify:
            return 'after_red_verify' if self.last_verify_red else 'after_green_verify'
        return 'plain'

    @staticmethod
    def failing_count(output):
        """pytest's summary, if any: 'N failed' → N; a green summary → 0; else None."""
        m = re.search(r'(\d+) failed', output or '')
        if m:
            return int(m.group(1))
        if re.search(r'\d+ passed', output or '') or str(output).startswith('exit 0'):
            return 0
        return None

    def think_now(self):
        """The per-turn rung: None when no policy is set (say nothing), else
        the decision under the policy."""
        if self.think_policy == 'on':
            return True
        if self.think_policy == 'off':
            return False
        # phase: diagnosis turns only
        return self.turn == 1 or (self.after_verify and self.last_verify_red)

    def think_budget(self):
        """Tokens of thinking this turn may spend: base x phase (turn one 1x,
        after a red verify 2x, two edits without a green 4x), never above the
        adaptive term's suggestion when that is higher, never above the ceiling,
        never more than a quarter of the remaining clock at the measured rate."""
        mult = 1
        if self.after_verify and self.last_verify_red:
            mult = 2
        if self.edits_since_green >= 2:
            mult = 4
        b = max(self.think_base * mult, self.think_next)
        b = min(b, self.think_ceiling)
        if self.max_seconds:
            left = max(0.0, self.max_seconds - (time.time() - self.t0))
            b = min(b, int(left * self.tok_rate / 4))
        return max(0, int(b))

    def _request_body(self):
        think = False if self.force_no_think else self.think_now()
        budget = self.think_budget() if think else 0
        self.current_budget = budget
        # H-07: one build of the schema (tools() consumes the turn's withhold),
        # and the lean prompt is rendered from that same array — the phase gate
        # and the withhold law remove a tool per turn, and a prompt that still
        # named it would be the system lying to the model about its own hands.
        tools = self.tools()
        if self.rules_policy == 'lean':
            self.messages[0]["content"] = system_prompt('lean', self.verify_cmd, tools)
        body = {"model": self.model, "messages": self.messages, "tools": tools,
                "temperature": self.temperature,
                "max_tokens": (self.ANSWER_ROOM + budget) if think else self.max_tokens}
        if think is not None:
            body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
        return body

    def _account_think(self, choice, elapsed, turn):
        """After a thinking turn: what it spent and how it closed, and the
        adaptive term for the next one. Returns True when the turn overflowed
        (cut by the soft budget before any action) and must be continued."""
        msg = choice.get('message', {}) or {}
        rc = msg.get('reasoning_content') or ''
        usage = choice.get('usage') or {}
        used = int(usage.get('completion_tokens') or len(rc) // 4)
        if rc and elapsed > 0.5:
            self.tok_rate = 0.5 * self.tok_rate + 0.5 * (used / elapsed)
        overflow = (choice.get('finish_reason') == 'length' and not msg.get('tool_calls')
                    and not (msg.get('content') or '').strip() and bool(rc))
        if overflow:
            closed = 'overflow'
            self.think_next = min(self.think_ceiling, max(self.current_budget, 1) * 2)
        elif 'budget reached' in rc.lower():
            closed = 'budget'
            self.think_next = min(self.think_ceiling, max(self.current_budget, 1) * 2)
        else:
            closed = 'self'
            self.think_next = (max(self.think_base, self.current_budget // 2)
                               if used < self.current_budget / 2 else self.current_budget)
        log(f"[beekeeper t{turn}] think used={used} closed={closed}")
        self.last_think = (self.current_budget, used, closed)
        return overflow, rc

    def request(self):
        built = self._request_body()
        body = lambda: json.dumps(built).encode()
        for attempt in range(4):
            if attempt: time.sleep((0, 3, 8, 20)[attempt])
            try:
                hdrs = {"Content-Type": "application/json"}
                if API_KEY: hdrs["Authorization"] = f"Bearer {API_KEY}"
                r = urllib.request.urlopen(urllib.request.Request(
                    self.url, body(), hdrs), timeout=600)
                d = json.loads(r.read())
                if 'error' in d or 'choices' not in d:
                    err = json.dumps(d.get('error', d))[:200]
                    log(f"[beekeeper] server error: {err}")
                    if any(k in err.lower() for k in ('context', 'prefill', 'too large')):
                        self.compact(hard=True)
                    continue
                return d['choices'][0]
            except urllib.error.HTTPError as e:
                code = e.code
                if code in (408, 429) or code >= 500:
                    log(f"[beekeeper] transient HTTP {code}, retrying"); continue
                log(f"[beekeeper] HTTP {code} — caller's fault, not retrying: {e.read().decode(errors='replace')[:150]}")
                return None
            except Exception as e:
                log(f"[beekeeper] stream died ({str(e)[:100]}) — resurrecting")
        return None

    def opportunities(self):
        """H-51 (instrument-law.md M-04): one documented place where every
        lever's OPPORTUNITY count sits beside what it did. A lever that acts at
        a gate is `unmeasured` when the gate was never reached — the net was
        called inert on pool v2 after firing in 0 of 28 runs, and only a
        hand-count of the transcripts showed no run had ever reached `done`
        green. Zero here is a measurement; an absent block is not.

            restart   stalls                             / restarts
            withhold  exhaustions, refusals              / withheld_turns
            board     board_rows                         / board_flips
            trend     trend_observations                 / trend_improvements, trend_regressions
            ladder    ladder_rungs                       / ladder_discovered
            net       net_baselines, net_gate_reached    / net_refusals
            think     turns                              / think_turns
            create    create_offers                      / create_taken
        """
        return {"turns": self.turn,
                "stalls": int(self.end_reason == "stalled"),
                "restarts": max(0, int(getattr(self, "attempt", 1)) - 1),
                "exhaustions": self.exhaustion_events,
                "refusals": sum(self.refused_count.values()),
                "withheld_turns": self.withheld_turns,
                "board_rows": len(self.board_rows),
                "board_flips": len(self.board_flips),
                # trend    the gate: verifies that MEASURED something / the moves
                "trend_observations": len(self.trend_history),
                "trend_improvements": sum(1 for a, b in zip(self.trend_history, self.trend_history[1:])
                                          if b[1] < a[1]),
                "trend_regressions": sum(1 for a, b in zip(self.trend_history, self.trend_history[1:])
                                         if b[1] > a[1]),
                # ladder   the gate: rungs on the board / the ones the output added
                "ladder_rungs": len(self.board_rows),
                "ladder_discovered": self.ladder_discovered,
                "net_baselines": self.net_baselines,
                "net_gate_reached": self.net_gate_reached,
                "net_refusals": self.net_refusals,
                "think_turns": sum(1 for _, t in self.think_log if t),
                "create_offers": self.create_offers,
                "create_taken": self.create_taken}

    def _end(self, reason, rc):
        self.end_reason = reason
        if self.spend_turn:
            self._spend(self.spend_turn); self.spend_turn = {}
        self._close_create_offer()       # a live offer is recorded before the end record
        self._spend({"kind": "end", "reason": reason, "rc": rc, "turns": self.turn,
                     "t": round(time.time() - self.t0, 2), "arena": self.arena,
                     "compactions": self.compactions, "model": self.model,
                     "think_policy": self.think_policy,
                     "net_policy": self.net_policy,      # the ring check reads it: net=off owes no baseline
                     # M-04: effects beside their opportunities, in every arm
                     # H-07: the arm's own instrument, measured (chars/2.5), not inferred
                     "rules_policy": self.rules_policy,
                     "prompt_tokens_est": round(sum(len(self.messages[i]["content"])
                                                    for i in (0, 1)) / 2.5),
                     "phase_policy": self.phase_policy, "phase_k": self.phase_k,
                     "first_edit_turn": self.first_edit_turn, "forced_edit": self.forced_edit,
                     "forced_turns": self.forced_turns, "red_after_edit": self.red_after_edit,
                     "edit_withheld_after_red": self.edit_withheld_after_red,
                     # M-04: opportunity counts, reported whether the lever was on
                     # or off — zero opportunities is a reading, not a silence
                     "alt": self.alt_policy, "alternations": self.alternations,
                     "no_progress_events": self.no_progress_events, "wanders": self.wanders,
                     # M-04: opportunities, not only effects — a lever that never
                     # had a gate to act at is unmeasured, never inert
                     "autoverifies": self.autoverifies,
                     "autoverify_skipped": self.autoverify_skipped,
                     "create_offers": self.create_offers, "create_taken": self.create_taken,
                     "opportunities": self.opportunities()})
        if self.net_snapshot:
            # the background baseline is a daemon thread: it dies with the process,
            # and the copy does not outlive the run either way
            shutil.rmtree(self.net_snapshot, ignore_errors=True)
            self.net_snapshot = None
        return rc

    def run(self, max_seconds=None):
        t0 = time.time()
        self.t0, self.max_seconds = t0, max_seconds
        nudges = 0
        for turn in range(1, MAX_TURNS + 1):
            if max_seconds and time.time() - t0 > max_seconds:
                log(f"[beekeeper] soft cap {max_seconds}s reached"); return self._end("time budget", 1)
            if self.spend_turn:
                self._spend(self.spend_turn)
            self.turn = turn
            think = self.think_now()
            self.last_think = (0, 0, None)
            self.spend_turn = {"kind": "turn", "turn": turn, "t": round(time.time() - t0, 2),
                               "phase": self.phase(), "think": bool(think) if think is not None else None,
                               "budget": self.think_budget() if think else 0, "used": 0, "closed": None,
                               "action": None, "sig": None, "refused": False, "verify": None,
                               "failing": None, "ctx_chars": self._size(), "compactions": self.compactions,
                               "max_tokens": None, "tok_rate": round(self.tok_rate, 1)}
            if think is not None:
                self.think_log.append((turn, bool(think)))
                log(f"[beekeeper t{turn}] think={'on' if think else 'off'}"
                    + (f" budget={self.think_budget()}" if think else ""))
            if self.board_policy == 'on': self._render_board(turn)
            self._phase_gate(turn)
            self._offer_create(turn)
            if self._size() > self.hard_limit: self.compact(hard=True)
            elif self._size() > self.compact_at: self.compact()
            t_req = time.time()
            choice = self.request()
            if choice is None: return self._end("model unreachable", 2)
            if think:
                overflow, partial = self._account_think(choice, time.time() - t_req, turn)
                if overflow:
                    self.messages.append({"role": "user", "content":
                        "[thinking budget reached — your reasoning so far:\n"
                        f"{partial[-1200:]}]\nAct now with a tool call."})
                    self.force_no_think = True
                    try:
                        choice = self.request()
                    finally:
                        self.force_no_think = False
                    if choice is None: return self._end("model unreachable", 2)
            self.spend_turn.update({"budget": self.last_think[0] or self.spend_turn["budget"],
                                    "used": self.last_think[1], "closed": self.last_think[2],
                                    "max_tokens": self.current_budget + self.ANSWER_ROOM if think else self.max_tokens})
            msg, finish = choice.get('message', {}), choice.get('finish_reason')
            calls = msg.get('tool_calls') or []
            text = (msg.get('content') or '').strip()
            # truncation is never an answer (hive)
            if finish == 'length':
                self.exhaustions += 1
                good = []
                for tc in calls:
                    try: json.loads(tc.get('function', {}).get('arguments') or '{}'); good.append(tc)
                    except ValueError: log("[beekeeper] dropped truncated tool-call JSON (never replayed)")
                calls = good
                self.max_tokens = min(2048, self.max_tokens + 512)
                if not calls:
                    if self.exhaustions >= 3:
                        # compact only under real context pressure — repeated output
                        # truncation with a small context is starvation, not fullness
                        if self._size() > self.compact_at: self.compact(hard=True)
                        self.exhaustions = 0
                    self.messages.append({"role": "user", "content":
                        "(your output hit the token limit before any complete tool call — "
                        "budget raised; keep thinking brief and go straight to the call)"})
                    continue
            else:
                self.exhaustions = 0
                self.max_tokens = max(self.tok_floor, self.max_tokens - 100)
            if msg.get('reasoning_content') and self.tok_floor < 2048:
                self.tok_floor = 2048
                self.max_tokens = max(self.max_tokens, 2048)
                log("[beekeeper] thinking model detected — token floor raised to 2048")
            if not calls and text:
                calls = salvage_tool_calls(text)
                if calls: log(f"[beekeeper t{turn}] salvaged {len(calls)} tool call(s) from prose")
            if text: log(f"[beekeeper t{turn}] {text[:250]}")
            if not calls:
                nudges += 1
                if nudges > NUDGE_LIMIT: log("[beekeeper] model stopped acting; ending"); return self._end("model stopped acting", 1)
                self.messages.append({"role": "assistant", "content": text[:400]})
                self.messages.append({"role": "user", "content":
                    "Reply with a tool call, not prose. Use done only when the submission is written."})
                continue
            self.messages.append({"role": "assistant", "content": text[:400] or None, "tool_calls": calls})
            single = len(calls) == 1      # collapse only a one-call turn: the pair is the context's tail
            for tc in calls:
                f = tc.get('function', {})
                name = f.get('name', '?')
                try: args = json.loads(f.get('arguments') or '{}')
                except ValueError: args = {}
                brief = str(args.get('command') or args.get('file_path') or '')[:80]
                log(f"[beekeeper t{turn}] {name}: {brief}")
                if name == 'done':
                    refusal = self.t_done(**args)
                    if refusal is None:
                        log(f"[beekeeper] DONE (verified): {args.get('summary', '')[:200]}")
                        return self._end("capped", 0)
                    log(f"[beekeeper t{turn}]   -> {refusal[:200]}")
                    self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": refusal})
                    continue
                sig = norm_sig(f"{name} {brief}")
                if name in ('edit', 'write', 'create'):
                    # an edit's identity is its content, not its path: two
                    # different edits to one file are a search, not a loop
                    sig += "@" + hashlib.sha1(json.dumps(
                        [args.get('old_str'), args.get('new_str'), args.get('content')]).encode()).hexdigest()[:10]
                # The stall law: an action that has returned the identical
                # result 3x is EXHAUSTED — refused before it runs, until an
                # edit or write changes the world. A note does not move a
                # greedy model over a near-static context (pub1 + the ctx128k
                # retest, 2026-09-04: full context intact, 23 "identical Nx"
                # notes in view, the same probe re-issued in a period-3 cycle
                # because this guard used to reset its own counter at 3).
                # The refusal is an affordance change, and it is remembered.
                self.spend_turn.update({"action": name, "sig": sig})
                if sig in self.exhausted:
                    self.stall_refusals += 1
                    self.spend_turn["refused"] = True
                    if self.alt_policy == 'on':
                        # a refusal is sterile by definition, and it opens a fresh
                        # detour window: the next one or two actions are a wander
                        self.alt_detours = 0
                        self.alt_trail.append((sig, True))
                    self.refused_count[sig] = self.refused_count.get(sig, 0) + 1
                    result = fail('blocked', f"refused — this exact call has returned the identical "
                                             f"result {self.exhausted[sig]}x and will not run again until "
                                             f"something changes (an edit or write clears it). Take a "
                                             f"different action: edit a file, read a different file, or "
                                             f"run a different command.")
                    log(f"[beekeeper t{turn}]   -> {str(result)[:140]}")
                    keep = self.exhausted_idx.get(sig)
                    if single and keep is not None and keep < len(self.messages):
                        # the attractor is the repetition itself: this call
                        # does not enter the context — the surviving copy
                        # carries the count
                        self.messages.pop()
                        self.messages[keep]["content"] = (
                            f"{self.last_result_text if keep == self.last_result_idx else ''}\n\n"
                            f"[this exact call has been made {self.exhausted[sig]}x with the identical "
                            f"result and refused {self.refused_count[sig]}x since — it will not run "
                            f"again until an edit or write changes something. Take a different "
                            f"action: edit a file, read a different file, or run a different command.]").strip()
                    else:
                        self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": str(result)})
                    self.withheld.add(name)
                    if self.stall_refusals >= STALL_LIMIT:
                        log(f"[beekeeper] stalled: {self.stall_refusals} consecutive refused repeats; ending")
                        return self._end("stalled", 3)
                    continue
                fn = getattr(self, f"t_{name}", None)
                try:
                    result = fn(**args) if fn else fail('args', f"unknown tool {name}")
                except TypeError as e:
                    result = fail('args', str(e))
                except Exception as e:
                    result = fail('exec', f"{type(e).__name__}: {e}")
                # H-15, the wander law: one or two unrelated actions between two
                # refusals are a detour, not a fresh start — the streak survives
                # them (arm E's pattern: refusal, one action, refusal, for sixty
                # turns). A third is a new line of work and clears it; a
                # successful edit or write always clears it: progress is never
                # punished, and that exemption is the stall law's, untouched.
                progress = name in ('edit', 'write') and not str(result).startswith('ERROR')
                if self.alt_policy != 'on':
                    self.stall_refusals, self.alt_detours = 0, 0
                elif progress:
                    self.alt_detours = 0
                    # the exemption is for PROGRESS, and the harness measures it: an
                    # edit clears the streak unless the last verify said in so many
                    # words that the edits before it moved nothing. It is never
                    # punished either way — no refusal, no exhaustion, no withhold.
                    if not self.alt_flat:
                        self.stall_refusals = 0
                else:
                    self.alt_detours += 1
                    if self.alt_detours > ALT_DETOUR:
                        self.stall_refusals, self.alt_detours = 0, 0
                    elif self.stall_refusals:
                        self.wanders += 1
                        self._spend({"kind": "alt", "turn": turn, "pattern": "wander",
                                     "detour": self.alt_detours, "sig": sig,
                                     "streak": self.stall_refusals})
                self.withheld.clear()            # something executed: the schema is whole again
                if name in ('read', 'bash'):
                    self.phase_edit_blocked = False   # H-10: a fresh observation re-opens edit
                cmd = str(args.get('command') or '')
                is_verify = name == 'bash' and bool(cmd) and (
                    (self.verify_cmd and self.verify_cmd.strip() in cmd) or 'pytest' in cmd or 'docker run' in cmd)
                self.after_verify = is_verify
                if is_verify:
                    self.last_verify_red = not str(result).startswith('exit 0')
                    # the board has rows to flip, or the trend/ladder have their
                    # own reason to read this observation (a ladder run starts
                    # with no rows at all — the output is where they come from)
                    if self.board_rows or self.trend_policy == 'on' or self.ladder_policy == 'on':
                        m0 = re.match(r'exit (\d+)', str(result))
                        self._observe(int(m0.group(1)) if m0 else (1 if self.last_verify_red else 0), str(result), turn)
                    self.spend_turn["verify"] = 'red' if self.last_verify_red else 'green'
                    self.spend_turn["failing"] = self.failing_count(str(result))
                    self.phase_verified = True       # H-10: a verify has now been observed
                    if self.last_verify_red and self.edits_since_verify:
                        self.red_after_edit += 1     # the gate's opportunity, counted in every arm
                        if self.phase_policy == 'on':
                            self.phase_edit_blocked = True
                            self.edit_withheld_after_red += 1
                    self.edits_since_verify = 0
                    if not self.last_verify_red:
                        self.edits_since_green = 0
                    if self.alt_policy == 'on' and self._alt_no_progress(str(result), turn) \
                            and self.stall_refusals >= STALL_LIMIT:
                        self.alt_stall = "edits that never moved the verify"
                changed_world = progress
                if name == 'bash':
                    self._scan_missing(str(result), turn)
                changed_world = name in ('edit', 'write', 'create') and not str(result).startswith('ERROR')
                if changed_world:
                    self.edits_since_green += 1
                    self.edits_since_verify += 1
                    if self.first_edit_turn is None:   # a refused or failed edit is not an edit
                        self.first_edit_turn, self.forced_edit = turn, bool(self.phase_forcing)
                    self.flat_edits += 1
                if changed_world and self.exhausted:
                    self.exhausted.clear()   # the world changed; a re-probe is new information
                # collapse-with-count (eiDOS): repetition rendered AS repetition —
                # never of a successful edit or write: progress is exempt even
                # when its confirmation text repeats
                rsha = hashlib.sha1(str(result).encode()).hexdigest()
                if self.alt_policy == 'on':
                    # sterile: this action returned the result it returned before —
                    # no new information, whoever asked in between (the period-1
                    # law only sees a repeat that is IMMEDIATELY consecutive)
                    sterile = self.sig_result.get(sig) == rsha
                    self.sig_result[sig] = rsha
                    self._alt_step(sig, sterile, progress, turn)
                    if self.stall_refusals >= STALL_LIMIT:
                        self.alt_stall = "an alternating cycle"
                repeated = collapsed = False
                if changed_world:
                    self.last_result, self.repeat_run = (sig, rsha), 0
                elif (sig, rsha) == self.last_result:
                    self.repeat_run += 1
                    n = self.repeat_run + 1
                    result = (f"[identical to your previous result — {n}x in a row. "
                              f"You already have this. Stop repeating and move on.]")
                    # A command that exits 0 while changing nothing is a
                    # stall the exit code cannot see: the sig_history pivot
                    # only fires on failures, so an idle success can loop
                    # forever. Repetition itself is the signal.
                    if n >= 3:
                        repeated = True
                        self.exhaustion_events += 1
                        self.exhausted[sig] = n
                        self.exhausted_idx[sig] = self.last_result_idx
                        self.withheld.add(name)
                    if single and self.last_result_idx is not None and self.last_result_idx < len(self.messages):
                        # collapse: the repeat leaves the context, the first
                        # copy carries the count — repetition in the context
                        # is the pattern a greedy model completes
                        self.messages.pop()
                        self.messages[self.last_result_idx]["content"] = (
                            f"{self.last_result_text}\n\n[this exact call has been made {n}x; the result "
                            f"was identical every time. You already have this. Stop repeating and move on.]")
                        collapsed = True
                else:
                    self.last_result, self.repeat_run = (sig, rsha), 0
                    self.last_result_text = str(result)
                log(f"[beekeeper t{turn}]   -> {str(result)[:140]}")
                if collapsed:
                    self.sig_history.append((sig, not str(self.last_result_text).startswith('ERROR')))
                    if repeated:
                        self.messages.append({"role": "user", "content":
                            "[pivot required: that action has produced the identical "
                            "result 3x and is now exhausted — it will be refused until an "
                            "edit or write changes something. Change METHOD entirely — a "
                            "different tool is usually the answer; check the tools you "
                            "have not tried yet.]"})
                        self.sig_history.clear()
                    continue
                idx = len(self.messages)
                if (sig, rsha) == self.last_result and self.repeat_run == 0:
                    self.last_result_idx = idx
                self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": str(result)})
                self._pin_red(idx, str(result))
                if is_verify:
                    self.last_verify_idx = idx      # H-05: never evicted head and tail
                if name == 'read' and self.last_full_read:
                    self.read_msgs[idx] = self.last_full_read
                if changed_world:
                    self._autoverify(turn)      # H-32: the edit's result, unasked
                # the harness moves the model (eiDOS): forced pivot on a closed path
                ok = not str(result).startswith('ERROR')
                self.sig_history.append((sig, ok))
                if repeated:
                    self.messages.append({"role": "user", "content":
                        "[pivot required: that action has produced the identical "
                        "result 3x and is now exhausted — it will be refused until an "
                        "edit or write changes something. Change METHOD entirely — a "
                        "different tool is usually the answer; check the tools you "
                        "have not tried yet.]"})
                    self.sig_history.clear()   # the counter is NOT reset: the streak is remembered
                    continue
                tail = self.sig_history[-3:]
                if len(tail) == 3 and len({s for s, _ in tail}) == 1 and not any(o for _, o in tail):
                    self.messages.append({"role": "user", "content":
                        "[pivot required: that exact action has now failed 3x. That path is closed. "
                        "Do NOT run it again — change METHOD entirely: different tool, different file, "
                        "or re-diagnose from the failing output above.]"})
                    self.sig_history.clear()
            if self.alt_note:
                # flushed after the turn's calls: the collapse machinery pops the
                # tail of self.messages, so nothing of ours may sit under it
                self.messages.append({"role": "user", "content": self.alt_note})
                self.alt_note = None
            if self.alt_stall:
                # the streak reached the limit through a pattern rather than a
                # refusal: the turn is finished and recorded, then the run ends
                log(f"[beekeeper] stalled: {self.stall_refusals} consecutive refused repeats "
                    f"({self.alt_stall}); ending")
                return self._end("stalled", 3)
        log("[beekeeper] turn limit reached"); return self._end("turn budget", 1)

def _may_restart(left, walls, floor=RESTART_FLOOR_S, policy='on'):
    """H-12: another episode starts only while the clock holds a median
    episode and at least the floor. No clock: the count is the only limit.
    Policy `floor` restarts whenever at least the floor remains — pool v2's
    stalls came late (7–157 s left) and the median rule refused all nine."""
    if left is None:
        return True
    if policy == 'floor':
        return left >= float(floor)
    need = max(float(floor), statistics.median(walls) if walls else 0.0)
    return left >= need


def run_attempts(make, max_seconds=None, limit=RESTART_LIMIT):
    """H-12, restart on stall. `make(attempt, notes, prev)` builds a worker
    over the same tree: the task text plus one note per earlier attempt.
    An episode that ends stalled (exit 3) restarts as a FRESH context — the
    proxy pins the sampler, so the context is the only thing a restart can
    vary — while the clock still holds a median episode. The restart skips
    the start verify and carries the net's baseline (`prev`). Returns the
    last attempt's exit code and the attempt records."""
    t0 = time.time()
    attempts, notes, prev = [], [], None
    for attempt in range(1, limit + 1):
        left = None if max_seconds is None else max(0.0, max_seconds - (time.time() - t0))
        bk = make(attempt, notes, prev)
        bk.attempt = attempt
        t_a = time.time()
        rc = bk.run(max_seconds=left)
        wall = time.time() - t_a
        sigs = sorted(bk.exhausted)[:4]
        edited = sorted(bk._changed_files())[:6]
        rec = {"attempt": attempt, "rc": rc, "reason": bk.end_reason, "turns": bk.turn,
               "wall": round(wall, 2), "sigs": sigs, "edited": edited}
        attempts.append(rec)
        if rc != 3 or bk.restart_policy not in ('on', 'floor'):
            break
        left = None if max_seconds is None else max(0.0, max_seconds - (time.time() - t0))
        ok = _may_restart(left, [a["wall"] for a in attempts], policy=bk.restart_policy) and attempt < limit
        bk._spend({"kind": "restart", "attempt": attempt, "reason": bk.end_reason, "turns": bk.turn,
                   "wall": round(wall, 2), "left": None if left is None else round(left, 1),
                   "restarting": bool(ok), "sigs": sigs, "edited": edited})
        if not ok:
            log(f"[beekeeper] stalled with {left if left is None else round(left)}s left — no time for another episode")
            break
        notes.append(f"[Attempt {attempt} stalled after {bk.turn} turns and {wall:.0f}s: it kept repeating "
                     f"{', '.join(sigs) or 'the same call'}. The tree carries its edits "
                     f"({', '.join(edited) or 'none'}). Do not repeat those calls; take a different approach.]")
        log(f"[beekeeper] restart {attempt + 1}: fresh context over the same tree, {round(left) if left is not None else 'no'}s left")
        prev = bk
    return attempts[-1]["rc"], attempts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arena', required=True)
    ap.add_argument('--prompt'); ap.add_argument('--prompt-file')
    ap.add_argument('--verify', help='override the auto-detected verify command')
    ap.add_argument('--no-verify', action='store_true', help='run UNGATED (done is the model\'s word)')
    ap.add_argument('--max-seconds', type=int)
    ap.add_argument('--base-url', default=DEF_BASE); ap.add_argument('--model', default=DEF_MODEL)
    a = ap.parse_args()
    task = a.prompt or open(a.prompt_file).read()
    verify = None if a.no_verify else (a.verify or auto_verify(os.path.realpath(a.arena)))
    log(f"[beekeeper] verify gate: {verify}" if verify else
        "[beekeeper] UNGATED — no check detected or --no-verify; done is the model's word")
    def make(attempt, notes, prev):
        text = task + ("\n\n" + "\n".join(notes) if notes else "")
        return Beekeeper(a.arena, text, verify_cmd=verify, base_url=a.base_url, model=a.model,
                         start_verify=(attempt == 1),
                         net_baseline=(prev.net_baseline if prev is not None else None))
    rc, _ = run_attempts(make, max_seconds=a.max_seconds)
    sys.exit(rc)

if __name__ == '__main__':
    main()
