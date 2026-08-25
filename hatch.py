#!/usr/bin/env python3
"""hatch — get beekeeper a brain. Detects your system, picks an honest tier,
downloads a model + server, and writes ~/.beekeeper/config.

Lanes:
  hatch.py --api          use any OpenAI-compatible cloud API (30 seconds, no downloads)
  hatch.py                local: auto-tier by RAM (llama.cpp everywhere)
  hatch.py --mlx          local, Apple Silicon fast lane (oMLX + mixed-precision quant)
  hatch.py --small        force the sub-8GB tier
  hatch.py --dry-run      show the plan, download nothing
"""
import argparse, json, os, platform, shutil, subprocess, sys, tarfile, time, urllib.request, zipfile

HOME = os.path.expanduser('~/.beekeeper')
PORT = 8008

def ram_gb():
    try:
        if platform.system() == 'Darwin':
            return int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'])) / 2**30
        for line in open('/proc/meminfo'):
            if line.startswith('MemTotal'): return int(line.split()[1]) / 2**20
    except Exception: pass
    return 0

TIERS = {
    'ling':  dict(name='Ling-3.0-tiny IQ4_XS (8B-A1.3B MoE)', size_gb=4.4, ctx=32768,
                  url='https://huggingface.co/bartowski/Ling-3.0-tiny-GGUF/resolve/main/Ling-3.0-tiny-IQ4_XS.gguf',
                  file='Ling-3.0-tiny-IQ4_XS.gguf'),
    'ling8': dict(name='Ling-3.0-tiny IQ4_XS — tight-RAM profile', size_gb=4.4, ctx=12288,
                  url='https://huggingface.co/bartowski/Ling-3.0-tiny-GGUF/resolve/main/Ling-3.0-tiny-IQ4_XS.gguf',
                  file='Ling-3.0-tiny-IQ4_XS.gguf'),
    'small': dict(name='LFM2.5-2.6B Q4_K_M (sub-8GB tier, strong tool-calling)', size_gb=1.7, ctx=16384,
                  url='https://huggingface.co/LiquidAI/LFM2.5-2.6B-GGUF/resolve/main/LFM2.5-2.6B-Q4_K_M.gguf',
                  file='LFM2.5-2.6B-Q4_K_M.gguf'),
}

def pick_tier(ram, force_small):
    if force_small or (ram and ram < 7.5): return 'small'
    if ram and ram < 11.5: return 'ling8'
    return 'ling'

def llama_asset():
    sysname, arch = platform.system(), platform.machine().lower()
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(arch, arch)
    if sysname == 'Darwin': return f'macos-{arch}', 'tar.gz'
    if sysname == 'Linux':  return f'ubuntu-{arch}', 'tar.gz'
    if sysname == 'Windows': return f'win-cpu-{arch}', 'zip'
    sys.exit(f"hatch: unsupported platform {sysname}")

def fetch(url, dest, label):
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    req = urllib.request.Request(url)
    if have: req.add_header('Range', f'bytes={have}-')
    with urllib.request.urlopen(req) as r:
        total = have + int(r.headers.get('Content-Length', 0))
        if have >= total > 0: print(f"  {label}: already complete"); return
        done, nxt, t0 = have, (have * 100 // max(total, 1) // 10 + 1) * 10, time.time()
        with open(dest, 'ab' if have else 'wb') as f:
            while True:
                c = r.read(4 * 2**20)
                if not c: break
                f.write(c); done += len(c)
                if total and done * 100 / total >= nxt:
                    el = max(time.time() - t0, 1)
                    print(f"  {label}: {nxt}%  {done/2**30:.2f}/{total/2**30:.2f} GiB  "
                          f"{(done-have)/2**20/el:.0f} MiB/s", flush=True)
                    nxt += 10
    print(f"  {label}: done ({os.path.getsize(dest)/2**30:.2f} GiB)")

def install_llamacpp():
    tag = urllib.request.urlopen(
        'https://github.com/ggml-org/llama.cpp/releases/latest/download/nightly-tag.txt'
    ).read().decode().strip()
    plat, ext = llama_asset()
    name = f'llama-{tag}-bin-{plat}.{ext}'
    url = f'https://github.com/ggml-org/llama.cpp/releases/download/{tag}/{name}'
    arc = os.path.join(HOME, name)
    print(f"server: llama.cpp {tag} ({plat})")
    fetch(url, arc, 'llama.cpp')
    dest = os.path.join(HOME, 'llama')
    shutil.rmtree(dest, ignore_errors=True); os.makedirs(dest)
    (tarfile.open(arc) if ext == 'tar.gz' else zipfile.ZipFile(arc)).extractall(dest)
    for root, _, files in os.walk(dest):
        for f in files:
            if f in ('llama-server', 'llama-server.exe'):
                p = os.path.join(root, f); os.chmod(p, 0o755); return p
    sys.exit("hatch: llama-server not found in the release archive")

def write_config(base_url, model, serve_cmd=None, ctx=None):
    os.makedirs(HOME, exist_ok=True)
    with open(os.path.join(HOME, 'config'), 'w') as f:
        f.write(f'BEEKEEPER_BASE_URL="{base_url}"\nBEEKEEPER_MODEL="{model}"\n')
        f.write('# BEEKEEPER_API_KEY="sk-..."   # cloud lane: put your key here or export it\n')
    if serve_cmd:
        with open(os.path.join(HOME, 'serve.sh'), 'w') as f:
            f.write('#!/bin/sh\n# honest window: this -c value is what the hardware actually delivers\n'
                    f'exec {serve_cmd}\n')
        os.chmod(os.path.join(HOME, 'serve.sh'), 0o755)
    print(f"\nconfig written: {HOME}/config" + (f"\nserver script:  {HOME}/serve.sh (ctx {ctx})" if serve_cmd else ''))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--api', action='store_true'); ap.add_argument('--mlx', action='store_true')
    ap.add_argument('--small', action='store_true'); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true', help='with --dry-run: emit the plan as JSON (for orchestrators)')
    ap.add_argument('--ram', type=float, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.api:
        write_config('https://api.openai.com/v1', 'gpt-5.2-mini')
        print("cloud lane ready. Put your key in the config (or export BEEKEEPER_API_KEY),\n"
              "change BASE_URL/MODEL for any other OpenAI-compatible provider, then just run: bee \"task\"")
        return
    ram = a.ram or ram_gb()
    tier_key = pick_tier(ram, a.small)
    t = TIERS[tier_key]
    apple = platform.system() == 'Darwin' and platform.machine() == 'arm64'
    if a.dry_run and a.json:
        print(json.dumps({"tier": tier_key, "name": t['name'], "model": t['file'].removesuffix('.gguf'),
                          "file": t['file'], "size_gb": t['size_gb'], "ctx": t['ctx'], "port": PORT,
                          "ram_gb": round(ram, 1), "os": platform.system(), "arch": platform.machine()}))
        return
    print(f"system: {platform.system()} {platform.machine()} · {ram:.0f} GB RAM -> tier: {t['name']}")
    if tier_key == 'ling8':
        print("  note: workable but tight — close heavy apps while the bee works; --small is the safe fallback")
    if a.mlx and not apple: sys.exit("hatch: --mlx is Apple Silicon only")
    if a.dry_run:
        if a.mlx: print("plan: oMLX venv + mlx-works/Ling-3.0-tiny-oQ4e (4.6 GiB, mixed-precision)")
        else: print(f"plan: llama.cpp release binary + {t['file']} ({t['size_gb']} GiB), serve at :{PORT} ctx {t['ctx']}")
        return
    os.makedirs(HOME, exist_ok=True)
    if a.mlx:
        venv = os.path.join(HOME, 'omlx')
        print("Apple fast lane: creating oMLX venv (this pulls real dependencies)...")
        subprocess.run([sys.executable, '-m', 'venv', venv], check=True)
        pip = os.path.join(venv, 'bin', 'pip')
        subprocess.run([pip, '-q', 'install', 'git+https://github.com/jundot/omlx.git'], check=True)
        subprocess.run([os.path.join(venv, 'bin', 'python'), '-c',
            "from huggingface_hub import snapshot_download;"
            f"snapshot_download('mlx-works/Ling-3.0-tiny-oQ4e', local_dir='{HOME}/models/Ling-3.0-tiny-oQ4e')"],
            check=True)
        write_config(f'http://127.0.0.1:{PORT}/v1', 'Ling-3.0-tiny-oQ4e',
                     f'{venv}/bin/omlx serve --model-dir {HOME}/models --port {PORT}', 'model-native')
    else:
        server = install_llamacpp()
        model_p = os.path.join(HOME, 'models', t['file'])
        os.makedirs(os.path.dirname(model_p), exist_ok=True)
        fetch(t['url'], model_p, t['file'])
        write_config(f'http://127.0.0.1:{PORT}/v1', os.path.splitext(t['file'])[0],
                     f'"{server}" -m "{model_p}" --port {PORT} -c {t["ctx"]} -ngl 99 --jinja -fa on', t['ctx'])
    print('\nnext: run `bee serve` in one terminal (or add it to your login items), then: bee "task"')

if __name__ == '__main__':
    main()
