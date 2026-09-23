#!/usr/bin/env python3
"""Compare local STT profiles on the same audio. No model downloads, no transcript logging.
Example: MODELS_DIR=./models PYTHONPATH=backend backend/.venv/bin/python scripts/ai/compare_asr.py
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.asr import transcribe
from app.audio import load_wav
from app.config import Settings
from app.ml import ModelRegistry


def cer(reference, hypothesis):
    def norm(s):
        return " ".join(re.sub(r"[^\w\s]", "", s.casefold().replace("ё", "е")).split())
    a, b = norm(reference), norm(hypothesis)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j-1] + (x != y)))
        prev = row
    return round(prev[-1] / max(1, len(a)), 4)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", nargs="+", default=["mixed", "kk_only", "meeting_ru_kk"])
    parser.add_argument("--language", choices=["auto", "ru", "kk"], default="auto")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "backend/tests/fixtures/ai"
    manifest = json.loads((root / "manifest.json").read_text())
    settings = Settings()
    model = ModelRegistry(settings).get("asr")
    results = []
    for name in args.fixtures:
        reference = " ".join(t["text"] for t in manifest["recordings"][name]["turns"])
        samples = load_wav(root / f"{name}.wav")
        for profile in ("standard", "refined"):
            start = time.monotonic()
            result = transcribe(model, samples, profile=profile,
                language=None if args.language == "auto" else args.language,
                allowed_languages=["ru", "kk", "en"])
            row = dict(fixture=name, profile=profile, language=args.language,
                seconds=round(time.monotonic()-start, 2),
                cer=cer(reference, " ".join(s.text for s in result.segments)))
            results.append(row)
            print(json.dumps(row), flush=True)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
