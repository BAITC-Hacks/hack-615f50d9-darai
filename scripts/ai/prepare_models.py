#!/usr/bin/env python3
"""Explicit, one-time download of DARAI speech models into MODELS_DIR.

This is the ONLY step that talks to the Hugging Face Hub. Runtime loads
weights from MODELS_DIR with HF_HUB_OFFLINE=1.

Usage (from repo root, backend venv):
    backend/.venv/bin/python scripts/ai/prepare_models.py [--models-dir ./models] [--only asr,embedding]

Gated pyannote repos (segmentation-3.0, speaker-diarization-3.1) require:
  1. accepting the conditions on their HF pages with your account;
  2. HF_TOKEN in the environment of THIS command only (never commit it).

Revisions are pinned; the downloaded layout is verified by check_models.py.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# repo_id, pinned revision, target dir (relative to MODELS_DIR), files, gated
MODELS = {
    "asr": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478",
            "faster-whisper-large-v3",
            ["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json"], False),
    "embedding": ("pyannote/wespeaker-voxceleb-resnet34-LM", "837717ddb9ff5507820346191109dc79c958d614",
                  "pyannote/wespeaker-voxceleb-resnet34-LM", ["config.yaml", "pytorch_model.bin"], False),
    "segmentation": ("pyannote/segmentation-3.0", "e66f3d3b9eb0873085418a7b813d3b369bf160bb",
                     "pyannote/segmentation-3.0", ["config.yaml", "pytorch_model.bin", "LICENSE"], True),
    "diarization": ("pyannote/speaker-diarization-3.1", "84fd25912480287da0247647c3d2b4853cb3ee5d",
                    "pyannote/speaker-diarization-3.1", ["config.yaml"], True),
}

APPROX_MB = {"asr": 3091, "embedding": 27, "segmentation": 6, "diarization": 1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", default=os.environ.get("MODELS_DIR", "models"))
    ap.add_argument("--only", default=",".join(MODELS), help="comma list: " + ",".join(MODELS))
    args = ap.parse_args()

    from huggingface_hub import hf_hub_download

    root = Path(args.models_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")  # weights never go to git

    keys = [k.strip() for k in args.only.split(",") if k.strip()]
    need_mb = sum(APPROX_MB[k] for k in keys)
    free_mb = shutil.disk_usage(root).free / 1e6
    print(f"models dir: {root}; need ~{need_mb} MB, free {free_mb:.0f} MB")
    if free_mb < need_mb * 1.2 + 2000:
        print("ERROR: not enough free disk space (need size*1.2 + 2 GB reserve)")
        return 2

    token = os.environ.get("HF_TOKEN") or None
    failed = []
    for key in keys:
        repo, rev, sub, files, gated = MODELS[key]
        if gated and not token:
            print(f"[{key}] SKIP: {repo} is gated; set HF_TOKEN and accept the model conditions")
            failed.append(key)
            continue
        target = root / sub
        target.mkdir(parents=True, exist_ok=True)
        try:
            for f in files:
                hf_hub_download(repo, f, revision=rev, local_dir=target, token=token)
            (target / "REVISION").write_text(f"{repo}@{rev}\n")
            print(f"[{key}] OK {repo}@{rev[:10]} -> {target}")
        except Exception as exc:  # report and continue with the other models
            print(f"[{key}] FAILED {repo}: {type(exc).__name__}: {str(exc)[:200]}")
            failed.append(key)
    # drop hub download metadata caches, keep only the weights
    for cache in root.rglob(".cache"):
        shutil.rmtree(cache, ignore_errors=True)
    if failed:
        print("not prepared:", ",".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
