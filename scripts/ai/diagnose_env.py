#!/usr/bin/env python3
"""Environment diagnostics for the DARAI AI pipeline. Prints no secrets.

Usage: backend/.venv/bin/python scripts/ai/diagnose_env.py [--models-dir models]
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as exc:  # tool missing / timeout
        return f"n/a ({type(exc).__name__})"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", default=os.environ.get("MODELS_DIR", "models"))
    args = ap.parse_args()
    print(f"os: {platform.platform()}  arch: {platform.machine()}  python: {sys.version.split()[0]}")
    print(f"cpu cores: {os.cpu_count()}")
    if sys.platform == "darwin":
        print(f"cpu: {sh(['sysctl', '-n', 'machdep.cpu.brand_string'])}")
        print(f"ram: {int(sh(['sysctl', '-n', 'hw.memsize']) or 0) / 2**30:.0f} GiB")
    elif Path("/proc/meminfo").exists():
        print("ram:", Path("/proc/meminfo").read_text().splitlines()[0])
    print(f"nvidia-smi: {sh(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader']) if shutil.which('nvidia-smi') else 'absent'}")
    try:
        import torch

        print(f"torch {torch.__version__}: cuda={torch.cuda.is_available()} "
              f"mps={getattr(torch.backends, 'mps', None) is not None and torch.backends.mps.is_available()}")
    except ImportError:
        print("torch: not installed")
    try:
        import ctranslate2

        print(f"ctranslate2 {ctranslate2.__version__}: cuda devices={ctranslate2.get_cuda_device_count()} "
              "(no Metal backend: faster-whisper runs on CPU on macOS)")
    except ImportError:
        print("ctranslate2: not installed")
    for p in ("pyannote.audio", "faster-whisper", "huggingface_hub", "numpy", "scipy", "pydantic", "httpx"):
        try:
            print(f"  {p}=={md.version(p)}")
        except md.PackageNotFoundError:
            print(f"  {p}: MISSING")
    for tool in ("ffmpeg", "ffprobe", "docker", "ollama", "soffice"):
        print(f"{tool}: {shutil.which(tool) or 'absent'}")
    root = Path(args.models_dir)
    root.mkdir(parents=True, exist_ok=True)
    print(f"models dir: {root.resolve()}  free disk: {shutil.disk_usage(root).free / 2**30:.1f} GiB")
    print(f"HF_TOKEN set: {'yes' if os.environ.get('HF_TOKEN') else 'no'} (needed only for prepare_models.py)")
    if shutil.which("ollama"):
        print("ollama models:")
        for line in sh(["ollama", "list"]).splitlines()[1:]:
            name = line.split()[0] if line.split() else ""
            flag = "  FORBIDDEN (cloud proxy)" if name.endswith(":cloud") or name.endswith("-cloud") else ""
            print(f"  {line}{flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
