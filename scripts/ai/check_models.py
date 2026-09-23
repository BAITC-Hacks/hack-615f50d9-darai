#!/usr/bin/env python3
"""Offline completeness check of local model artifacts (no network, no Hub).

Loads each model with HF_HUB_OFFLINE=1 and every non-loopback socket blocked,
so a hidden Hub download fails loudly. Exit 0 only when all models load.

Usage (from backend/): MODELS_DIR=../models .venv/bin/python ../scripts/ai/check_models.py [--no-load]
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


def block_network() -> None:
    real = socket.socket.connect

    def guarded(self, addr):
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            raise OSError(f"network blocked by check_models: {host}")
        return real(self, addr)

    socket.socket.connect = guarded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-load", action="store_true", help="only check files")
    args = ap.parse_args()
    block_network()
    from app.config import get_settings
    from app.ml import ModelRegistry, embedding_checkpoint, segmentation_path

    s = get_settings()
    files = {
        "asr": [s.resolved_asr_path / f for f in ("model.bin", "config.json", "tokenizer.json",
                                                   "vocabulary.json", "preprocessor_config.json")],
        "diarization": [s.resolved_diarization_config, segmentation_path(s) / "pytorch_model.bin"],
        "embedding": [embedding_checkpoint(s)],
    }
    ok = True
    for key, paths in files.items():
        missing = [str(p) for p in paths if not p.is_file()]
        rev = next((p.parent / "REVISION" for p in paths if (p.parent / "REVISION").is_file()), None)
        print(f"[{key}] files: {'OK' if not missing else 'MISSING ' + ', '.join(missing)}"
              f"{'  rev=' + rev.read_text().strip() if rev else ''}")
        ok &= not missing
    if args.no_load:
        return 0 if ok else 1
    reg = ModelRegistry(s)
    for key in ("embedding", "diarization", "asr"):
        t0 = time.perf_counter()
        try:
            reg.get(key)
            print(f"[{key}] load: OK ({time.perf_counter() - t0:.1f}s, offline)")
        except Exception as exc:
            ok = False
            print(f"[{key}] load: FAILED {getattr(exc, 'message', type(exc).__name__)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
