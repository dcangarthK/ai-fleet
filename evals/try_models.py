#!/usr/bin/env python3
"""Try candidate models one at a time without filling the disk: pull, evaluate, keep only winners.

  python3 evals/try_models.py qwen2.5-coder:7b qwen3:8b [--keep-all] [--min-free-gb 5]

A model is kept if, after its run, it is the best 16GB-fitting model for at least one role (or with
--keep-all). Only models this script pulled are ever deleted; models you already had are left alone even if
they lose. Each decision is appended to evals/results/try_models.log.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results" / "results.json"
LOG = HERE / "results" / "try_models.log"
PULLED = HERE / "results" / "pulled_by_try_models.json"  # only these may ever be removed
ROLES = ("coder", "reviewer", "fast")


def ollama(*args: str, check: bool = True) -> str:
    return subprocess.run(["ollama", *args], capture_output=True, text=True, check=check).stdout


def installed() -> set[str]:
    lines = ollama("list").splitlines()[1:]
    return {line.split()[0] for line in lines if line.strip()}


def free_gb() -> float:
    return shutil.disk_usage(Path.home()).free / 1e9


def leaders() -> dict[str, str]:
    """role -> best model that fits a 16GB laptop."""
    results = json.loads(RESULTS.read_text()) if RESULTS.exists() else []
    best = {}
    for role in ROLES:
        fits = [r for r in results if r.get("fits_16gb") and role in r.get("roles", {})]
        if fits:
            best[role] = max(fits, key=lambda r: r["roles"][role]["score"])["model"]
    return best


def pulled() -> set[str]:
    return set(json.loads(PULLED.read_text())) if PULLED.exists() else set()


def remember(names: set[str]) -> None:
    PULLED.parent.mkdir(parents=True, exist_ok=True)
    PULLED.write_text(json.dumps(sorted(names)))


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M')} {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="+")
    ap.add_argument("--keep-all", action="store_true")
    ap.add_argument("--min-free-gb", type=float, default=5, help="stop if a pull would leave less than this free")
    args = ap.parse_args()
    for model in args.models:
        had_it = model in installed() or f"{model}:latest" in installed()
        if not had_it:
            log(f"pull {model} ({free_gb():.0f}GB free)")
            if subprocess.run(["ollama", "pull", model]).returncode != 0:
                log(f"pull failed: {model}; skipping")
                continue
            remember(pulled() | {model})
            if free_gb() < args.min_free_gb:
                log(f"only {free_gb():.0f}GB free after pulling {model}; removing it and stopping")
                ollama("rm", model, check=False)
                remember(pulled() - {model})
                return 1
        run = subprocess.run([sys.executable, str(HERE / "run_evals.py"), "--models", model])
        if run.returncode != 0:
            log(f"eval failed for {model}")
        if args.keep_all:
            log(f"keep {model} (--keep-all)")
            continue
        # Prune every model this script pulled that leads no role, including earlier winners just beaten.
        best = set(leaders().values())
        for name in sorted(pulled()):
            if name not in best:
                ollama("rm", name, check=False)
                remember(pulled() - {name})
                log(f"removed {name}: not the best 16GB model for any role (leaders: {leaders()})")
        if model in best:
            log(f"keep {model}: best 16GB model for {', '.join(r for r, m in leaders().items() if m == model)}")
        elif had_it:
            log(f"keep {model} (was already installed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
