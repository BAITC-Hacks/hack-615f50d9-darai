#!/usr/bin/env python3
"""Simulate a live MediaRecorder session on a speech file with the REAL preview.

WAV -> one WebM/Opus container (1 s clusters, like MediaRecorder) -> bytes
arrive in chunks of --chunk-seconds; after every --every-seconds of new audio
the backend would take a fixed snapshot (prefix copy) and call
live_ai.transcribe_preview with the previous result. Optional --realtime
paces chunks with the wall clock to measure lag honestly. Then the full
container goes through the unchanged final pipeline (process_recording).

Usage (from backend/):
  MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 .venv/bin/python ../scripts/ai/live_simulate.py \\
      tests/fixtures/ai/meeting_ru_kk.wav [--realtime] [--final] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


def encode_webm(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1",
                    "-c:a", "libopus", "-b:a", "32k", "-cluster_time_limit", "1000", "-f", "webm", str(dst)],
                   check=True)


def simulate(wav: Path, *, chunk_seconds=2.0, every_seconds=4.0, realtime=False, work: Path | None = None,
             registry=None) -> dict:
    from app.live_ai import transcribe_preview
    from app.live_ai_types import LivePreviewRequest

    work = work or Path(tempfile.mkdtemp(prefix="darai-live-"))
    container = work / "session.webm"
    encode_webm(wav, container)
    data = container.read_bytes()
    import soundfile as sf

    duration = sf.info(str(wav)).duration
    n_chunks = max(1, int(round(duration / chunk_seconds)))
    step = len(data) // n_chunks + 1
    chunks = [data[i:i + step] for i in range(0, len(data), step)]
    prev = None
    calls = []
    first_text_at = None
    t_start = time.perf_counter()
    arrived = 0
    i = -1
    while True:
        if realtime:
            # chunks available by the WALL CLOCK: chunk k arrives at (k + 1) * chunk_seconds
            now = time.perf_counter() - t_start
            avail = min(len(chunks), int(now // chunk_seconds))
            if avail <= arrived:
                time.sleep((avail + 1) * chunk_seconds - now)
                continue
            if avail < len(chunks) and (avail - arrived) * chunk_seconds < every_seconds:
                time.sleep(max(0.0, (arrived + every_seconds / chunk_seconds) * chunk_seconds - now))
                continue
            arrived = avail
        else:
            arrived = min(len(chunks), arrived + max(1, int(round(every_seconds / chunk_seconds))))
        i = arrived - 1
        last = arrived == len(chunks)
        received = b"".join(chunks[:arrived])
        snap = work / f"snap-{i}.webm"          # backend: fixed prefix copy, never the growing file
        snap.write_bytes(received)
        t0 = time.perf_counter()
        prev = transcribe_preview(LivePreviewRequest(snap, "audio/webm;codecs=opus", len(received),
                                                     work / "ai", prev), registry=registry)
        wall = time.perf_counter() - t0
        audio_in = min(duration, arrived * chunk_seconds)
        text = " ".join(u.text for u in prev.utterances)
        if text and first_text_at is None:
            first_text_at = {"chunk": i, "of": len(chunks), "audio_received": round(audio_in, 1),
                             "wall": round(time.perf_counter() - t_start, 1)}
        now_audio = min(duration, (time.perf_counter() - t_start)) if realtime else audio_in
        calls.append({"chunk": i, "audio_received": round(audio_in, 1), "decoded": prev.decoded_seconds,
                      "processed_until": prev.processed_until_seconds, "status": prev.preview_status,
                      "error": prev.preview_error, "asr_seconds": prev.asr_seconds, "call_seconds": round(wall, 2),
                      "lag": round(now_audio - prev.processed_until_seconds, 1),
                      "final": sum(u.is_final for u in prev.utterances), "total": len(prev.utterances)})
        snap.unlink()
        if last:
            break
    data = b"".join(chunks)
    # after stop: drain remaining audio with the full container (strictly the same bytes)
    for _ in range(10):
        if prev is None or prev.processed_until_seconds >= prev.decoded_seconds - 0.05:
            break
        prev = transcribe_preview(LivePreviewRequest(container, "audio/webm", len(data), work / "ai", prev),
                                  registry=registry)
    return {"duration": round(duration, 2), "chunks": len(chunks), "calls": calls, "first_text": first_text_at,
            "container": str(container), "result": prev}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--chunk-seconds", type=float, default=2.0)
    ap.add_argument("--every-seconds", type=float, default=4.0)
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--final", action="store_true", help="run process_recording on the full container")
    ap.add_argument("--json")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg required")
        return 2
    r = simulate(Path(args.wav), chunk_seconds=args.chunk_seconds, every_seconds=args.every_seconds,
                 realtime=args.realtime)
    for c in r["calls"]:
        print(f"chunk {c['chunk']:3d} audio={c['audio_received']:6.1f}s decoded={c['decoded']:6.1f}s "
              f"processed={c['processed_until']:6.1f}s lag={c['lag']:5.1f}s "
              f"asr={c['asr_seconds']:5.2f}s call={c['call_seconds']:5.2f}s {c['status']} "
              f"final={c['final']}/{c['total']} {c['error'] or ''}")
    print("first text:", r["first_text"])
    print("\nPREVIEW (your terminal only):")
    for u in r["result"].utterances:
        print(f"  {u.id} [{u.start:6.2f}-{u.end:6.2f}] {'F' if u.is_final else '~'} {u.text}")
    if args.json:
        Path(args.json).write_text(json.dumps({k: v for k, v in r.items() if k != "result"} |
                                              {"preview": r["result"].to_dict()}, ensure_ascii=False, indent=2))
    if args.final:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from app.ai_pipeline import get_pipeline
        from app.ai_types import AIError, MeetingContext, ProcessRequest

        req = ProcessRequest(source_path=Path(r["container"]), work_dir=Path(r["container"]).parent / "final",
                             meeting=MeetingContext(datetime.now(ZoneInfo("Asia/Almaty")), "Asia/Almaty", []),
                             candidates=[], on_stage=lambda st: print("  final stage:", st), run_extraction=False)
        try:
            res = get_pipeline().process_recording(req)
            print(f"FINAL ok: {len(res.utterances)} utterances, speakers={[s.label for s in res.speakers]}")
        except AIError as exc:
            print(f"FINAL {exc.code} stage={exc.stage} model={exc.model}: {exc.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
