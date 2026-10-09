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
import argparse, hashlib, json, os, platform, shutil, subprocess, sys, tarfile, time, urllib.request, zipfile

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

HF = 'https://huggingface.co/%s/resolve/main/%s'
TIERS = {
    'qwen4b': dict(name='Qwen3.5-4B IQ4_XS', size_gb=2.5, ctx=32768,
                   url=HF % ('unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-IQ4_XS.gguf'),
                   file='Qwen3.5-4B-IQ4_XS.gguf',
                   sha256='658a9e7e406deb06d0179755e3c14f6a82915a4be4962a2f92a64d948d2e572f',
                   sampler=dict(temp=0.7, top_p=0.8, top_k=20, min_p=0, presence_penalty=1.5)),
    'qwen4b_tight': dict(name='Qwen3.5-4B IQ4_XS — tight-RAM profile', size_gb=2.5, ctx=12288,
                   url=HF % ('unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-IQ4_XS.gguf'),
                   file='Qwen3.5-4B-IQ4_XS.gguf',
                   sha256='658a9e7e406deb06d0179755e3c14f6a82915a4be4962a2f92a64d948d2e572f',
                   sampler=dict(temp=0.7, top_p=0.8, top_k=20, min_p=0, presence_penalty=1.5)),
    'gemma': dict(name='Gemma-4-26B-A4B UD-IQ3_S (MoE, 4B active)', size_gb=11.3, ctx=32768,
                  url=HF % ('unsloth/gemma-4-26B-A4B-it-GGUF', 'gemma-4-26B-A4B-it-UD-IQ3_S.gguf'),
                  file='gemma-4-26B-A4B-it-UD-IQ3_S.gguf',
                  sha256='878be93f9c238ea853b3fd1eb602637ce3cf1cddea56dc345d9a7bf2d6093e29',
                  sampler=dict(temp=1.0, top_p=0.95, top_k=64, min_p=0)),
    # optional, chosen only with --architect: a dense 27B reasoning model that wants a 16 GB+ GPU
    # (think with chat_template_kwargs reasoning_effort "low" for tool turns, "medium" for decisions;
    # see BEEKEEPER_EFFORT_EXPLORE / BEEKEEPER_EFFORT_DECIDE)
    'architect': dict(name='Swift-Qwen3.8-27B IQ3_XS (dense 27B reasoning, 16 GB+ GPU)', size_gb=13.0, ctx=32768,
                  url=HF % ('ukisai/Swift-Qwen3.8-27B-GGUF', 'Swift-Qwen3.8-27B-IQ3_XS.gguf'),
                  file='Swift-Qwen3.8-27B-IQ3_XS.gguf',
                  sha256='b71a91573e4f77cec646563b7578dd65b17e5939347e80a732ae12a5111b66c7',
                  sampler=dict(temp=1.0, top_p=0.95, top_k=20, min_p=0)),
}

def pick_tier(ram, force_small, architect=False):
    if architect: return 'architect'
    if force_small or (ram and ram < 11.5): return 'qwen4b_tight'
    if ram and ram < 15.5: return 'qwen4b'
    return 'gemma'

def sampler_flags(t):
    s = t['sampler']
    return (f"--temp {s['temp']} --top-p {s['top_p']} --top-k {s['top_k']} --min-p {s['min_p']}"
            + (f" --presence-penalty {s['presence_penalty']}" if 'presence_penalty' in s else ''))

def verify_sha256(path, want):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''): h.update(chunk)
    if h.hexdigest() != want:
        sys.exit(f"hatch: sha256 mismatch for {os.path.basename(path)} (got {h.hexdigest()}, want {want}); delete it and retry")

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
    ap.add_argument('--small', action='store_true'); ap.add_argument('--architect', action='store_true', help='the optional 27B reasoning tier (16 GB+ GPU)'); ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true', help='with --dry-run: emit the plan as JSON (for orchestrators)')
    ap.add_argument('--ram', type=float, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.api:
        write_config('https://api.openai.com/v1', 'gpt-5.2-mini')
        print("cloud lane ready. Put your key in the config (or export BEEKEEPER_API_KEY),\n"
              "change BASE_URL/MODEL for any other OpenAI-compatible provider, then just run: bee \"task\"")
        return
    ram = a.ram or ram_gb()
    tier_key = pick_tier(ram, a.small, a.architect)
    t = TIERS[tier_key]
    apple = platform.system() == 'Darwin' and platform.machine() == 'arm64'
    if a.dry_run and a.json:
        print(json.dumps({"tier": tier_key, "name": t['name'], "model": t['file'].removesuffix('.gguf'),
                          "file": t['file'], "size_gb": t['size_gb'], "sha256": t['sha256'], "ctx": t['ctx'], "port": PORT,
                          "ram_gb": round(ram, 1), "os": platform.system(), "arch": platform.machine()}))
        return
    print(f"system: {platform.system()} {platform.machine()} · {ram:.0f} GB RAM -> tier: {t['name']}")
    if tier_key == 'qwen4b_tight':
        print("  note: workable but tight — close heavy apps while the bee works")
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
        verify_sha256(model_p, t['sha256'])
        write_config(f'http://127.0.0.1:{PORT}/v1', os.path.splitext(t['file'])[0],
                     f'"{server}" -m "{model_p}" --port {PORT} -c {t["ctx"]} -ngl 99 --jinja -fa on {sampler_flags(t)}', t['ctx'])
    print('\nnext: run `bee serve` in one terminal (or add it to your login items), then: bee "task"')

if __name__ == '__main__':
    main()
