#!/usr/bin/env python3
"""SIMULATED ~1.5 min live-preview fixture (macOS TTS Milena ru_RU / Aru kk_KZ).

Covers what the live preview must not break: negations ("буду" / "не буду",
"барамын" / "бармаймын"), the same phrase repeated later, a long pause, Kazakh
and mixed turns, a long sentence crossing window boundaries. Synthetic voices,
not a quality claim for real meetings.

Output: backend/tests/fixtures/ai/live_long.wav + live_long.json
Usage: python3 scripts/ai/make_live_fixture.py   (macOS, needs `say` and ffmpeg)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_fixtures import KK, OUT, RU, SR, read_pcm, tts, write_pcm  # noqa: E402

# (voice, lang, text, pause AFTER the turn in seconds)
TURNS = [
    (RU, "ru", "Коллеги, начинаем планёрку.", 0.6),
    (RU, "ru", "Я буду готовить отчёт по закупкам.", 0.6),
    (KK, "kk", "Мен бүгін есепті дайындаймын.", 0.6),
    (RU, "ru", "Я не буду готовить презентацию.", 6.0),          # long pause: silence inside the meeting
    (RU, "ru", "Коллеги, начинаем планёрку.", 0.6),               # same phrase again, later
    (KK, "kk", "Мен ертең жиналысқа бармаймын.", 0.6),
    (KK, "kk", "Ал Дана ертең жиналысқа барады.", 0.6),
    (KK, "mixed", "Жарайды, отчётты жұмаға дейін жіберемін.", 0.6),
    (RU, "ru", "Сергей будет отвечать за бюджет, а Марат не будет.", 0.4),
    (RU, "ru", "Также нужно согласовать график поставок оборудования с подрядчиком "
               "и проверить все договоры до конца месяца.", 0.6),
    (RU, "ru", "Всем спасибо, совещание окончено.", 1.0),
]
SPEAKER = {RU: "anna", KK: "dana"}


def main() -> int:
    pcm = b"\x00\x00" * int(0.5 * SR)
    ref = []
    with tempfile.TemporaryDirectory() as td:
        for i, (voice, lang, text, pause) in enumerate(TURNS):
            p = Path(td) / f"{i}.wav"
            tts(voice, text, p)
            start = len(pcm) / 2 / SR
            pcm += read_pcm(p)
            ref.append({"speaker": SPEAKER[voice], "lang": lang, "text": text,
                        "start": round(start, 3), "end": round(len(pcm) / 2 / SR, 3)})
            pcm += b"\x00\x00" * int(pause * SR)
    write_pcm(OUT / "live_long.wav", pcm)
    meta = {"note": "SIMULATED TTS voices; not real speakers", "file": "live_long.wav",
            "duration": round(len(pcm) / 2 / SR, 3), "turns": ref}
    (OUT / "live_long.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(f"live_long.wav {meta['duration']} s, {len(ref)} turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
