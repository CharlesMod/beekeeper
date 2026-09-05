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
import argparse, hashlib, json, os, re, signal, subprocess, sys, time
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

def log(msg): print(msg, flush=True)

def fail(kind, msg):
    assert kind in FAIL_KINDS
    return f"ERROR[{kind}]: {msg}"

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
    def __init__(self, arena, task, verify_cmd=None, base_url=DEF_BASE, model=DEF_MODEL):
        self.arena = os.path.realpath(arena)
        self.url = base_url.rstrip('/').removesuffix('/chat/completions').removesuffix('/v1') + '/v1/chat/completions'
        self.model = model
        self.verify_cmd = verify_cmd
        budget = self._load_budget()
        self.compact_at = int(budget * 0.55) * 4      # chars
        self.hard_limit = int(budget * 0.80) * 4
        log(f"[beekeeper] context budget {budget} tokens ({self.budget_source}); "
            f"compact at {self.compact_at} chars, hard at {self.hard_limit}")
        self.max_tokens = 700
        self.tok_floor = 700                 # raised to 2048 when a thinking model is detected
        self.exhaustions = 0
        self.read_cache = {}
        self.read_msgs = {}                  # message idx -> path, so eviction can un-cache reads
        self.last_full_read = None
        self.ledger = []
        self.sig_history = []            # (norm_sig, ok) trail for loop pivot
        self.poison = {}                 # norm_sig -> crash count
        self.override_pending = set()    # (path, old_str, new_str) numeric-gate refusals awaiting re-issue
        self.last_result = (None, None)  # (sig, sha) for collapse-with-count
        self.repeat_run = 0
        self.exhausted = {}              # norm_sig -> identical-result count; refused until an edit/write
        self.stall_refusals = 0          # consecutive refused repeats
        self.pin_idx = set()             # message indices compaction must never evict
        anchored = (f"[Arena root: {self.arena} — your bash commands run there. "
                    f"Use relative paths; never leave it.]\n\n{task}")
        self.messages = [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": anchored}]
        self.pin_idx.update({0, 1})
        self.snapshot = self._tree_hash()
        self.protected = self._protected_set()
        self.assert_base = self._assert_count()
        if verify_cmd:
            code, _ = self._run_verify()
            if code == 0:
                log("[beekeeper] WARNING: verify already green at start — a check that cannot fail cannot gate")

    # ---------- tamper monitors (hive quorum) ----------
    def _tree_hash(self):
        out = {}
        for root, _, files in os.walk(self.arena):
            for f in files:
                p = os.path.join(root, f)
                try: out[os.path.relpath(p, self.arena)] = hashlib.sha1(open(p, 'rb').read()).hexdigest()
                except OSError: pass
        return out

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

    def t_read(self, file_path):
        p, note = self._inside(file_path)
        if not p: return fail('blocked', f"{file_path} is outside the arena and nothing in it matches that filename")
        try: body = open(p, errors='replace').read()
        except OSError as e: return fail('args', str(e))
        sha = hashlib.sha1(body.encode()).hexdigest()
        self.last_full_read = None
        if self.read_cache.get(p) == sha:
            return "[unchanged since your last read — you already have this file in context]"
        self.read_cache[p] = sha
        self.last_full_read = p
        lines = body.splitlines()
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
        before = s.encode()
        prev_mtime = os.stat(p).st_mtime
        open(p, 'w').write(s.replace(old_str, new_str))
        err = self._syntax_guard(p, before)
        if err: return err
        self._freshen(p, prev_mtime)
        self.read_cache.pop(p, None)
        tag = " [numeric-only, applied on override]" if numeric_only else ""
        self.ledger.append(f"edit {os.path.basename(p)}: {old_str.strip()[:50]!r} -> {new_str.strip()[:50]!r}{tag}")
        if numeric_only:
            return ("OK: replaced 1 occurrence. NOTE: this edit changed only numeric literals and was "
                    "applied on your override. If you are tuning a constant to satisfy a test, that is "
                    "the wrong fix — find the wrong OPERATION (sign, comparison, name) instead.")
        return (note or '') + "OK: replaced 1 occurrence"

    def t_write(self, file_path, content):
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
        self.ledger.append(f"write {os.path.basename(p)} ({len(content)} chars)")
        return (note or '') + f"OK: wrote {len(content)} chars"

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
                out, _ = p.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                try: os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError: p.kill()
                out, _ = p.communicate()
                self.poison[sig] = self.poison.get(sig, 0) + 1
                return fail('timeout', "command timed out at 60s (process tree killed). "
                                       "Non-interactive commands only; everything must exit on its own.")
        except OSError as e:
            return fail('exec', str(e))
        if p.returncode < 0 or p.returncode >= 126:
            self.poison[sig] = self.poison.get(sig, 0) + 1
        out = (out or '').strip() or "(no output)"
        if len(out) > 3500: out = out[:3500] + "\n... [truncated]"
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

    def _run_verify(self):
        try:
            r = subprocess.run(self.verify_cmd, shell=True, cwd=self.arena, capture_output=True,
                               text=True, timeout=120, stdin=subprocess.DEVNULL)
            return r.returncode, (r.stdout + r.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            return 124, "verify timed out"

    def t_done(self, summary):
        v = self.tamper_violations()
        if v:
            return fail('blocked', "done refused — tamper monitor: " + "; ".join(v) +
                                   ". Restore the protected files; the specification is not yours to edit.")
        if self.verify_cmd:
            code, out = self._run_verify()
            if code != 0:
                return fail('blocked', f"done refused — verify exited {code}. The work is not done:\n{out}")
        return None  # signals acceptance

    # ---------- context discipline ----------
    def _size(self):
        return sum(len(str(m.get('content') or '')) + len(str(m.get('tool_calls') or '')) + 200
                   for m in self.messages)

    def compact(self, hard=False):
        keep = 4 if hard else 8
        n = len(self.messages)
        evictable = [i for i in range(2, n - keep) if i not in self.pin_idx]
        if not evictable: return
        dropped = 0
        for i in evictable:
            m = self.messages[i]
            if m.get('role') == 'tool' and len(str(m.get('content') or '')) > 200:
                m['content'] = "(evicted to fit context — re-run the tool if needed)"
                dropped += 1
                # evicting a read result means the model no longer has that file:
                # un-cache it so the re-run this notice asks for actually serves content
                if i in self.read_msgs:
                    self.read_cache.pop(self.read_msgs.pop(i), None)
        if dropped:
            self.last_result, self.repeat_run = (None, None), 0
        ledger = '\n'.join(self.ledger[-30:]) or '(none yet)'
        self.messages.insert(2, {"role": "user", "content":
            f"[context compacted: {dropped} old tool results elided. Action ledger:\n{ledger}\n"
            f"Do not re-read unchanged files.]"})
        self.pin_idx = {i + 1 if i >= 2 else i for i in self.pin_idx}
        self.read_msgs = {i + 1 if i >= 2 else i: p for i, p in self.read_msgs.items()}
        log(f"[beekeeper] compacted ({'hard' if hard else 'soft'}): {dropped} results elided")

    def _pin_red(self, idx, content):
        """Never evict the most recent failing verify/test output (hive NEVER_TRIM)."""
        low = content[:500].lower()
        if ('failed' in low or 'error' in low) and ('test' in low or 'exit 1' in low or 'exit 2' in low):
            self.pin_idx = {i for i in self.pin_idx if i in (0, 1)} | {idx}

    # ---------- model ----------
    def request(self):
        body = lambda: json.dumps({"model": self.model, "messages": self.messages, "tools": TOOLS,
                                   "temperature": 0.2, "max_tokens": self.max_tokens}).encode()
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

    def run(self, max_seconds=None):
        t0 = time.time()
        nudges = 0
        for turn in range(1, MAX_TURNS + 1):
            if max_seconds and time.time() - t0 > max_seconds:
                log(f"[beekeeper] soft cap {max_seconds}s reached"); return 1
            if self._size() > self.hard_limit: self.compact(hard=True)
            elif self._size() > self.compact_at: self.compact()
            choice = self.request()
            if choice is None: return 2
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
                if nudges > NUDGE_LIMIT: log("[beekeeper] model stopped acting; ending"); return 1
                self.messages.append({"role": "assistant", "content": text[:400]})
                self.messages.append({"role": "user", "content":
                    "Reply with a tool call, not prose. Use done only when the submission is written."})
                continue
            self.messages.append({"role": "assistant", "content": text[:400] or None, "tool_calls": calls})
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
                        return 0
                    log(f"[beekeeper t{turn}]   -> {refusal[:200]}")
                    self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": refusal})
                    continue
                sig = norm_sig(f"{name} {brief}")
                if name in ('edit', 'write'):
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
                if sig in self.exhausted:
                    self.stall_refusals += 1
                    result = fail('blocked', f"refused — this exact call has returned the identical "
                                             f"result {self.exhausted[sig]}x and will not run again until "
                                             f"something changes (an edit or write clears it). Take a "
                                             f"different action: edit a file, read a different file, or "
                                             f"run a different command.")
                    log(f"[beekeeper t{turn}]   -> {str(result)[:140]}")
                    self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": str(result)})
                    if self.stall_refusals >= STALL_LIMIT:
                        log(f"[beekeeper] stalled: {self.stall_refusals} consecutive refused repeats; ending")
                        return 3
                    continue
                fn = getattr(self, f"t_{name}", None)
                try:
                    result = fn(**args) if fn else fail('args', f"unknown tool {name}")
                except TypeError as e:
                    result = fail('args', str(e))
                except Exception as e:
                    result = fail('exec', f"{type(e).__name__}: {e}")
                self.stall_refusals = 0
                changed_world = name in ('edit', 'write') and not str(result).startswith('ERROR')
                if changed_world and self.exhausted:
                    self.exhausted.clear()   # the world changed; a re-probe is new information
                # collapse-with-count (eiDOS): repetition rendered AS repetition —
                # never of a successful edit or write: progress is exempt even
                # when its confirmation text repeats
                rsha = hashlib.sha1(str(result).encode()).hexdigest()
                repeated = False
                if changed_world:
                    self.last_result, self.repeat_run = (sig, rsha), 0
                elif (sig, rsha) == self.last_result:
                    self.repeat_run += 1
                    result = (f"[identical to your previous result — {self.repeat_run + 1}x in a row. "
                              f"You already have this. Stop repeating and move on.]")
                    # A command that exits 0 while changing nothing is a
                    # stall the exit code cannot see: the sig_history pivot
                    # only fires on failures, so an idle success can loop
                    # forever. Repetition itself is the signal.
                    if self.repeat_run + 1 >= 3:
                        repeated = True
                        self.exhausted[sig] = self.repeat_run + 1
                else:
                    self.last_result, self.repeat_run = (sig, rsha), 0
                log(f"[beekeeper t{turn}]   -> {str(result)[:140]}")
                idx = len(self.messages)
                self.messages.append({"role": "tool", "tool_call_id": tc.get('id', ''), "content": str(result)})
                self._pin_red(idx, str(result))
                if name == 'read' and self.last_full_read:
                    self.read_msgs[idx] = self.last_full_read
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
        log("[beekeeper] turn limit reached"); return 1

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
    bk = Beekeeper(a.arena, task, verify_cmd=verify, base_url=a.base_url, model=a.model)
    sys.exit(bk.run(max_seconds=a.max_seconds))

if __name__ == '__main__':
    main()
