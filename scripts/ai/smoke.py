#!/usr/bin/env python3
"""Process an audio file through the REAL pipeline without UI (no DB, no HTTP).

Usage (from backend/):
  MODELS_DIR=../models LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen3:8b \\
  .venv/bin/python ../scripts/ai/smoke.py tests/fixtures/ai/meeting_ru_kk.wav \\
      --enroll "Иванова Анна=tests/fixtures/ai/enroll_ru_milena.wav" \\
      --enroll "Ахметова Дана=tests/fixtures/ai/enroll_kk_aru.wav" \\
      --date 2026-09-23T10:00 --tz Asia/Almaty [--no-llm] [--out result.json]

Prints the transcript to YOUR terminal only (nothing is logged); a missing
model is reported as a blocker with exit code 3, never faked.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--enroll", action="append", default=[], help='"ФИО=path/to/sample.wav"')
    ap.add_argument("--participant", action="append", default=[], help="ФИО без голосового профиля")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%dT%H:%M"))
    ap.add_argument("--tz", default="Asia/Almaty")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()

    from app.ai_pipeline import AIPipeline
    from app.ai_types import AIError, MeetingContext, Participant, ProcessRequest, VoiceCandidate, VoiceQualityRejected

    p = AIPipeline()
    work = Path(tempfile.mkdtemp(prefix="darai-smoke-"))
    participants, candidates = [], []
    for spec in args.enroll:
        name, _, path = spec.partition("=")
        emp = uuid.uuid5(uuid.NAMESPACE_URL, name)
        participants.append(Participant(emp, name))
        t0 = time.perf_counter()
        try:
            r = p.enroll_voice(Path(path), work)
        except VoiceQualityRejected as exc:
            print(f"[enroll] {name}: REJECTED {exc.reasons} {exc.message}")
            continue
        except AIError as exc:
            print(f"BLOCKER [{exc.code}] {exc.message}")
            return 3
        print(f"[enroll] {name}: {r.quality_status} {r.reasons} speech={r.speech_seconds}s "
              f"consistency={r.consistency} ({time.perf_counter() - t0:.1f}s)")
        candidates.append(VoiceCandidate(emp, r.vector))
    for name in args.participant:
        participants.append(Participant(uuid.uuid5(uuid.NAMESPACE_URL, name), name))
    names = {x.employee_id: x.fio for x in participants}

    meeting = MeetingContext(datetime.fromisoformat(args.date).replace(tzinfo=ZoneInfo(args.tz)), args.tz, participants)
    t0 = time.perf_counter()
    try:
        res = p.process_recording(ProcessRequest(
            source_path=Path(args.audio), work_dir=work, meeting=meeting, candidates=candidates,
            on_stage=lambda st: print(f"  stage: {st}", flush=True), run_extraction=not args.no_llm))
    except AIError as exc:
        print(f"BLOCKER [{exc.code}] stage={exc.stage} model={exc.model}: {exc.message}")
        return 3
    total = time.perf_counter() - t0
    print(f"\ndone in {total:.1f}s for {res.duration_seconds:.1f}s audio (x{total / max(res.duration_seconds, 1e-9):.2f});"
          f" languages={res.languages}; timings={[(t.stage, t.seconds) for t in res.timings]}")
    print("\nSPEAKERS")
    for sp in res.speakers:
        who = names.get(sp.proposed_employee_id, "Неизвестный")
        print(f"  {sp.label}: {who} sim={sp.similarity} 2nd={sp.second_similarity} review={sp.review_reasons} "
              f"clean={sp.clean_speech_seconds}s")
    print("\nTRANSCRIPT")
    for u in res.utterances:
        flag = f" ⚠{u.uncertain_reasons}" if u.uncertain_reasons else ""
        print(f"  #{u.index} [{u.start:6.1f}-{u.end:6.1f}] {u.speaker_label} ({u.language}): {u.text}{flag}")
    ex = res.extraction
    print(f"\nEXTRACTION: {ex.status} {ex.error_code or ''} {ex.error_message or ''}")
    for t in ex.tasks:
        print(f"  - {t.task} | кому: {names.get(t.to_employee_id)} | от: {t.from_label} | срок: {t.deadline} "
              f"({t.deadline_source}) | src={t.source_ids} conf={t.confidence} {t.review_reasons}")
    if ex.summary:
        print("\nSUMMARY\n" + ex.summary)
    if args.out:
        Path(args.out).write_text(json.dumps(dataclasses.asdict(res), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
