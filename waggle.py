#!/usr/bin/env python3
"""waggle — dance a task at the hive, from any device on the tailnet.

A single-file task board for beekeeper: queue tasks from phone or terminal,
one serial worker runs them (one GPU, one bee), live transcripts stream back.
Reachable only from localhost and the tailnet (100.64.0.0/10) by IP check.
"""
import ipaddress, json, os, re, subprocess, threading, time, uuid, html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = 8484
HOME = os.path.expanduser('~/.waggle')
LOGS = os.path.join(HOME, 'logs')
STATE_F = os.path.join(HOME, 'tasks.json')
BEE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'beekeeper.py')
ALLOW = [ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('::1/128'),
         ipaddress.ip_network('100.64.0.0/10')]
os.makedirs(LOGS, exist_ok=True)

LOCK = threading.Lock()
def load():
    try: return json.load(open(STATE_F))
    except Exception: return {'tasks': [], 'arenas': []}
def save(st):
    tmp = STATE_F + '.tmp'
    json.dump(st, open(tmp, 'w'), indent=1); os.replace(tmp, STATE_F)
STATE = load()

def worker():
    while True:
        with LOCK:
            t = next((x for x in STATE['tasks'] if x['status'] == 'queued'), None)
            if t: t['status'] = 'running'; t['started'] = time.time(); save(STATE)
        if not t:
            time.sleep(2); continue
        logf = os.path.join(LOGS, t['id'] + '.log')
        cmd = ['python3', BEE, '--arena', t['arena'], '--prompt', t['prompt'],
               '--max-seconds', str(t.get('cap', 900))]
        if t.get('ungated'): cmd.append('--no-verify')
        elif t.get('verify'): cmd += ['--verify', t['verify']]
        try:
            with open(logf, 'w', buffering=1) as lf:
                p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                     text=True, stdin=subprocess.DEVNULL)
                code = p.wait()
        except Exception as e:
            open(logf, 'a').write(f"\n[waggle] worker error: {e}\n"); code = 3
        tail = ''
        try: tail = open(logf, errors='replace').read()[-400:]
        except OSError: pass
        with LOCK:
            t['status'] = 'done' if code == 0 else 'incomplete' if code == 1 else 'error'
            t['exit'] = code; t['finished'] = time.time()
            t['verified'] = code == 0 and 'DONE (verified)' in tail
            save(STATE)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>waggle</title>
<style>
:root{--abyss:#071119;--water:#0D1F2C;--deep:#0A1822;--foam:#D7E9EF;--mist:#7391A1;
--honey:#E8C27A;--pass:#3E9C64;--fail:#C25454;
--mono:'IBM Plex Mono',ui-monospace,Menlo,monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--abyss);color:var(--foam);font-family:var(--mono);font-size:15px;
padding:18px;max-width:680px;margin:0 auto;line-height:1.5}
h1{font-size:22px;letter-spacing:-.01em}h1 b{color:var(--honey)}
.sub{color:var(--mist);font-size:11.5px;margin:2px 0 16px}
form{display:grid;gap:9px;background:var(--water);border:1px solid #16303F;
border-radius:10px;padding:14px}
textarea,input[type=text]{background:var(--deep);border:1px solid #24455A;color:var(--foam);
font:inherit;border-radius:7px;padding:10px;width:100%}
textarea{min-height:84px;resize:vertical}
label{font-size:10.5px;letter-spacing:.12em;color:var(--mist);text-transform:uppercase}
.row{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--mist)}
button{background:var(--honey);color:var(--abyss);border:0;border-radius:8px;
padding:12px;font:inherit;font-weight:600;cursor:pointer;font-size:15px}
.task{background:var(--water);border:1px solid #16303F;border-left:4px solid var(--honey);
border-radius:9px;padding:10px 13px;margin-top:10px}
.task a{color:var(--foam);text-decoration:none;display:block}
.tp{font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm{font-size:10.5px;color:var(--mist);margin-top:2px}
.st{float:right;font-size:10px;letter-spacing:.1em;border:1px solid #24455A;
border-radius:99px;padding:2px 8px;color:var(--mist)}
.st.running{color:var(--abyss);background:var(--honey);border-color:var(--honey)}
.st.done{color:#9FD8B4;border-color:var(--pass)}
.st.incomplete,.st.error{color:#E8A2A2;border-color:var(--fail)}
pre{background:var(--deep);border-radius:8px;padding:12px;font-size:11px;
white-space:pre-wrap;word-break:break-word;margin-top:12px;max-height:70vh;overflow-y:auto}
.back{color:var(--mist);font-size:12px;text-decoration:none}
</style></head><body>%BODY%</body></html>"""

def board():
    with LOCK: st = json.loads(json.dumps(STATE))
    arenas = st['arenas'][:6] or [os.path.expanduser('~/Software')]
    opts = ''.join(f'<option value="{html.escape(a)}">' for a in arenas)
    rows = ''
    for t in reversed(st['tasks'][-20:]):
        age = time.strftime('%H:%M', time.localtime(t['created']))
        v = ' ·verified' if t.get('verified') else (' ·ungated' if t.get('ungated') else '')
        rows += (f'<div class="task"><a href="/t/{t["id"]}">'
                 f'<span class="st {t["status"]}">{t["status"]}</span>'
                 f'<div class="tp">{html.escape(t["prompt"][:90])}</div>'
                 f'<div class="tm">{age} · {html.escape(os.path.basename(t["arena"]))}{v}</div></a></div>')
    return PAGE.replace('%BODY%', f"""
<h1>🐝 <b>waggle</b></h1><div class="sub">dance a task at the hive · beekeeper runs it · one at a time</div>
<form method="POST" action="/task">
<label>task</label><textarea name="prompt" required placeholder="fix the failing tests in ..."></textarea>
<label>arena directory</label><input type="text" name="arena" list="ar" required placeholder="/Users/cmod/Software/...">
<datalist id="ar">{opts}</datalist>
<div class="row"><input type="checkbox" name="ungated" id="u"><label for="u" style="text-transform:none">ungated (skip verification — not recommended)</label></div>
<button>⟶ hand it to the bee</button></form>
<div id="tasks">{rows or '<div class="sub" style="margin-top:14px">no tasks yet — the hive is quiet</div>'}</div>
<script>setTimeout(()=>location.reload(), 5000)</script>""")

def task_page(tid):
    with LOCK: t = next((x for x in STATE['tasks'] if x['id'] == tid), None)
    if not t: return PAGE.replace('%BODY%', '<a class="back" href="/">← board</a><p>unknown task</p>')
    try: log = open(os.path.join(LOGS, tid + '.log'), errors='replace').read()[-12000:]
    except OSError: log = '(no output yet)'
    aut = '<script>setTimeout(()=>location.reload(), 4000)</script>' if t['status'] in ('queued','running') else ''
    return PAGE.replace('%BODY%', f"""
<a class="back" href="/">← board</a>
<h1 style="font-size:16px;margin-top:8px">{html.escape(t['prompt'][:120])}</h1>
<div class="sub">{html.escape(t['arena'])} · <span class="st {t['status']}" style="float:none">{t['status']}</span>
{' · exit '+str(t.get('exit')) if 'exit' in t else ''}{' · ✓ verified' if t.get('verified') else ''}</div>
<pre>{html.escape(log)}</pre>{aut}""")

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _allowed(self):
        try: ip = ipaddress.ip_address(self.client_address[0])
        except ValueError: return False
        return any(ip in n for n in ALLOW)
    def _send(self, body, code=200, ctype='text/html; charset=utf-8'):
        b = body.encode(); self.send_response(code)
        self.send_header('Content-Type', ctype); self.send_header('Content-Length', len(b))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if not self._allowed(): return self._send('403: not on the tailnet', 403)
        if self.path == '/': return self._send(board())
        m = re.match(r'^/t/([0-9a-f-]+)$', self.path)
        if m: return self._send(task_page(m.group(1)))
        if self.path == '/api': 
            with LOCK: return self._send(json.dumps(STATE['tasks'][-20:]), ctype='application/json')
        self._send('404', 404)
    def do_POST(self):
        if not self._allowed(): return self._send('403: not on the tailnet', 403)
        if self.path != '/task': return self._send('404', 404)
        q = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
        prompt = (q.get('prompt') or [''])[0].strip()
        arena = os.path.realpath(os.path.expanduser((q.get('arena') or [''])[0].strip()))
        if not prompt or not os.path.isdir(arena):
            return self._send(PAGE.replace('%BODY%',
                '<a class="back" href="/">← board</a><p>need a task and a real arena directory</p>'))
        t = {'id': str(uuid.uuid4())[:8], 'prompt': prompt, 'arena': arena,
             'ungated': bool(q.get('ungated')), 'status': 'queued', 'created': time.time()}
        with LOCK:
            STATE['tasks'].append(t)
            if arena in STATE['arenas']: STATE['arenas'].remove(arena)
            STATE['arenas'].insert(0, arena); STATE['arenas'] = STATE['arenas'][:10]
            save(STATE)
        self.send_response(303); self.send_header('Location', f'/t/{t["id"]}'); self.end_headers()

if __name__ == '__main__':
    with LOCK:  # crash recovery: a task mid-run when waggle died is honestly marked
        for t in STATE['tasks']:
            if t['status'] == 'running': t['status'] = 'error'; t['exit'] = -1
        save(STATE)
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
