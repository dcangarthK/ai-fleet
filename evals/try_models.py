#!/usr/bin/env python3
"""Try candidate models one at a time without filling the disk: pull, evaluate, keep only winners.

  python3 evals/try_models.py qwen2.5-coder:7b qwen3:8b [--keep-all] [--min-free-gb 8]

Safety rules:
- Before each pull, the download size is read from the Ollama registry; the pull is skipped unless
  size + --min-free-gb fits on disk.
- Only models pulled by *this* run can be deleted. Anything installed when the run started is never touched.
- A pulled model is kept if it is a best (ties included) 16GB-fitting model for at least one role among the
  models installed now, judged on a fresh, successful evaluation from this run. Otherwise it's removed.
- If a removal fails, the run stops.
Decisions are appended to evals/results/try_models.log.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "results.json"
LOG = HERE / "results" / "try_models.log"
ROLES = ("coder", "reviewer", "fast")
REGISTRY = "https://registry.ollama.ai/v2"


def canonical(name: str) -> str:
    """Ollama's own naming: a missing tag means ':latest'."""
    return name if ":" in name.split("/")[-1] else f"{name}:latest"


def installed() -> set[str]:
    out = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=True).stdout
    return {canonical(line.split()[0]) for line in out.splitlines()[1:] if line.strip()}


def free_gb() -> float:
    return shutil.disk_usage(Path.home()).free / 1e9


def download_gb(model: str) -> float | None:
    """Total layer size from the registry manifest, or None if it can't be read."""
    repo, tag = canonical(model).rsplit(":", 1)
    repo = repo if "/" in repo else f"library/{repo}"
    req = urllib.request.Request(f"{REGISTRY}/{repo}/manifests/{tag}",
                                 headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            m = json.load(r)
        return (sum(layer.get("size", 0) for layer in m.get("layers", [])) + m.get("config", {}).get("size", 0)) / 1e9
    except (OSError, ValueError):
        return None


def results() -> dict[str, dict]:
    return {canonical(r["model"]): r for r in json.loads(RESULTS.read_text())} if RESULTS.exists() else {}


def leaders(present: set[str]) -> dict[str, set[str]]:
    """role -> every installed, 16GB-fitting model with the top score (ties all lead)."""
    rows = [r for name, r in results().items() if name in present and r.get("fits_16gb")]
    out = {}
    for role in ROLES:
        scored = [r for r in rows if role in r.get("roles", {})]
        if scored:
            top = max(r["roles"][role]["score"] for r in scored)
            out[role] = {canonical(r["model"]) for r in scored if r["roles"][role]["score"] == top}
    return out


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def remove(model: str) -> bool:
    out = subprocess.run(["ollama", "rm", model], capture_output=True, text=True)
    if out.returncode:
        log(f"could not remove {model}: {out.stderr.strip()[:200]}; stopping so nothing is left untracked")
    return out.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="+")
    ap.add_argument("--keep-all", action="store_true")
    ap.add_argument("--min-free-gb", type=float, default=8, help="disk that must stay free after a pull")
    args = ap.parse_args()
    at_start = installed()
    pulled_now: set[str] = set()
    for model in map(canonical, args.models):
        if model not in at_start:
            size = download_gb(model)
            if size is None:
                log(f"skip {model}: couldn't read its size from the registry")
                continue
            if free_gb() < size + args.min_free_gb:
                log(f"skip {model}: needs {size:.1f}GB + {args.min_free_gb:.0f}GB reserve, {free_gb():.1f}GB free")
                continue
            log(f"pull {model} ({size:.1f}GB, {free_gb():.1f}GB free)")
            if subprocess.run(["ollama", "pull", model]).returncode != 0:
                log(f"pull failed: {model}")
                if free_gb() < args.min_free_gb:
                    log("disk reserve reached after a failed pull; stopping")
                    return 1
                continue
            pulled_now.add(model)
        before = results().get(model, {}).get("evaluated_at")
        run = subprocess.run([sys.executable, str(HERE / "run_evals.py"), "--models", model])
        fresh = results().get(model)
        if run.returncode != 0 or not fresh or fresh.get("evaluated_at") == before or fresh.get("errors"):
            log(f"eval failed or incomplete for {model}; not judging it")
            if model in pulled_now and not args.keep_all:
                if not remove(model):
                    return 1
                pulled_now.discard(model)
                log(f"removed {model} (unjudged)")
            continue
        if args.keep_all:
            log(f"keep {model} (--keep-all)")
            continue
        best = leaders(installed())
        winning = {m for ms in best.values() for m in ms}
        # Prune this run's pulls that lead nothing, including earlier winners a newer model just beat.
        for name in sorted(pulled_now - winning):
            if not remove(name):
                return 1
            pulled_now.discard(name)
            log(f"removed {name}: not a best 16GB model for any role")
        if model in winning:
            log(f"keep {model}: best 16GB model for {', '.join(r for r, ms in best.items() if model in ms)}")
        elif model in at_start:
            log(f"keep {model} (was installed before this run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
