#!/usr/bin/env python3
"""Simulate a live MediaRecorder session on speech files with the REAL preview.

WAV(s) -> one WebM/Opus container (1 s clusters, like MediaRecorder) -> bytes
arrive in chunks of --chunk-seconds. The consumer behaves like the backend: it
takes a fixed prefix snapshot of the bytes received so far, calls
live_ai.transcribe_preview with the previous result and publishes every
on_update; it calls again at once while has_pending_audio, waits
retry_after_ms when the model is busy, otherwise waits for a new chunk.

--realtime: a separate producer thread appends chunks by the wall clock
(scaled by --speed), independently of ASR: the "microphone" never pauses while
the model is busy, so lag is honest. Without it, chunks are fed call by call
(deterministic, for tests).

Metrics: time to first text, per-window ASR time, audio/ASR speed, lag,
latency from the end of each reference phrase to its first publication and to
its final publication, CER against the reference (fixtures with a manifest).

Usage (from backend/):
  MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 .venv/bin/python ../scripts/ai/live_simulate.py \\
      tests/fixtures/ai/live_long.wav tests/fixtures/ai/meeting_ru_kk.wav --realtime [--language auto|ru|kk] \\
      [--final] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
FIX = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "ai"


def encode_webm(src: Path, dst: Path) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-ac", "1",
                    "-c:a", "libopus", "-b:a", "32k", "-cluster_time_limit", "1000", "-f", "webm", str(dst)],
                   check=True)


def reference(wavs: list[Path]) -> tuple[Path | None, list[dict], float]:
    """Concatenate WAVs; reference turns shifted to the common timeline (if a manifest knows them)."""
    import numpy as np
    import soundfile as sf

    man = json.loads((FIX / "manifest.json").read_text()) if (FIX / "manifest.json").is_file() else {}
    turns, parts, offset = [], [], 0.0
    for w in wavs:
        x, sr = sf.read(str(w), dtype="float32")
        assert sr == 16000, "16 kHz WAV expected"
        meta = FIX / f"{w.stem}.json"
        src_turns = (json.loads(meta.read_text())["turns"] if meta.is_file()
                     else (man.get("recordings", {}).get(w.stem) or {}).get("turns", []))
        turns += [dict(t, start=t["start"] + offset, end=t["end"] + offset) for t in src_turns]
        parts.append(x)
        offset += len(x) / sr
    if len(wavs) == 1:
        return wavs[0], turns, offset
    out = Path(tempfile.mkdtemp(prefix="darai-ref-")) / "concat.wav"
    sf.write(str(out), np.concatenate(parts), 16000, subtype="PCM_16")
    return out, turns, offset


# ------------------------------------------------------------------ metrics


def norm(s: str) -> str:
    s = re.sub(r"[^\w ]+", " ", s.lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", s).strip()


def cer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, a in enumerate(r, 1):
        cur = [i]
        for j, b in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != b)))
        prev = cur
    return prev[-1] / len(r)


def turn_cer(ref: str, hyp_full: str) -> tuple[float, str]:
    """CER of a reference turn against the best-matching word span of the whole preview text
    (utterances may merge or split turns, so time-based assignment is unreliable)."""
    r, words = norm(ref), norm(hyp_full).split()
    n_ref = max(1, len(r.split()))
    best, best_txt = 1.0, ""
    for i in range(len(words)):
        for k in range(max(1, n_ref - 2), n_ref + 3):
            cand = " ".join(words[i:i + k])
            c = cer(r, cand)
            if c < best:
                best, best_txt = c, cand
    return best, best_txt


def phrase_latency(events: list[dict], turns: list[dict], speed: float) -> list[dict]:
    """First publication covering the end of each turn (any / final text), relative to the
    wall-clock moment the phrase ended in the simulated microphone stream."""
    out = []
    for t in turns:
        spoken_end = t["end"] / speed
        first = final = None
        for e in events:
            if first is None and any(u["end"] >= t["end"] - 0.6 and u["start"] <= t["end"] for u in e["utts"]):
                first = e["wall"]
            if final is None and any(u["final"] and u["end"] >= t["end"] - 0.6 and u["start"] <= t["end"]
                                     for u in e["utts"]):
                final = e["wall"]
        out.append({"text": t["text"][:40], "lang": t["lang"], "end": round(t["end"], 2),
                    "first_s": None if first is None else round(first - spoken_end, 1),
                    "final_s": None if final is None else round(final - spoken_end, 1)})
    return out


# ------------------------------------------------------------------ simulation


def simulate(wav: Path | list[Path], *, chunk_seconds=2.0, every_seconds=4.0, realtime=False, speed=1.0,
             work: Path | None = None, registry=None, language: str | None = None, poll_seconds=0.25) -> dict:
    from app.live_ai import transcribe_preview
    from app.live_ai_types import LivePreviewRequest

    wavs = [Path(w) for w in (wav if isinstance(wav, (list, tuple)) else [wav])]
    src, turns, duration = reference(wavs)
    work = work or Path(tempfile.mkdtemp(prefix="darai-live-"))
    container = work / "session.webm"
    encode_webm(src, container)
    data = container.read_bytes()
    n_chunks = max(1, int(round(duration / chunk_seconds)))
    step = len(data) // n_chunks + 1
    chunks = [data[i:i + step] for i in range(0, len(data), step)]

    received = {"n": 0}                  # chunks appended so far (producer)
    t_start = time.perf_counter()
    wall = lambda: time.perf_counter() - t_start  # noqa: E731

    def producer():
        for k in range(len(chunks)):
            delay = (k + 1) * chunk_seconds / speed - wall()
            if delay > 0:
                time.sleep(delay)
            received["n"] = k + 1

    if realtime:
        threading.Thread(target=producer, daemon=True).start()

    events: list[dict] = []
    calls: list[dict] = []
    prev = None
    fed = 0
    snap_i = 0
    call_asr = {"last": 0.0}

    def publish(r):
        # backend: persist + publish
        window_asr, call_asr["last"] = r.asr_seconds - call_asr["last"], r.asr_seconds
        events.append({"wall": wall(), "processed": r.processed_until_seconds, "asr": round(window_asr, 2),
                       "audio_in": min(duration, received["n"] * chunk_seconds),
                       "utts": [{"start": u.start, "end": u.end, "final": u.is_final} for u in r.utterances],
                       "text": " ".join(u.text for u in r.utterances)})

    while True:
        if not realtime and not (prev is not None and prev.has_pending_audio):
            fed = min(len(chunks), fed + max(1, int(round(every_seconds / chunk_seconds))))
            received["n"] = fed
        n = received["n"]
        done = n == len(chunks)
        if n == 0:
            time.sleep(poll_seconds)
            continue
        blob = b"".join(chunks[:n])
        snap = work / f"snap-{snap_i}.webm"   # fixed prefix copy, never the growing file
        snap_i += 1
        snap.write_bytes(blob)
        t0 = time.perf_counter()
        n_ev = len(events)
        call_asr["last"] = 0.0
        prev = transcribe_preview(LivePreviewRequest(snap, "audio/webm;codecs=opus", len(blob), work / "ai", prev,
                                                     asr_language=language, on_update=publish), registry=registry)
        snap.unlink()
        call_ev = events[n_ev:]
        audio_in = min(duration, n * chunk_seconds)
        calls.append({"chunk": n - 1, "audio_received": round(audio_in, 1), "decoded": prev.decoded_seconds,
                      "processed_until": prev.processed_until_seconds, "status": prev.preview_status,
                      "error": prev.preview_error, "asr_seconds": prev.asr_seconds,
                      "call_seconds": round(time.perf_counter() - t0, 2), "windows": len(call_ev),
                      "pending": prev.has_pending_audio, "retry_after_ms": prev.retry_after_ms,
                      "lag": round(min(duration, n * chunk_seconds) - prev.processed_until_seconds, 1),
                      "final": sum(u.is_final for u in prev.utterances), "total": len(prev.utterances)})
        if prev.has_pending_audio:
            continue                      # same backlog, no new chunk needed
        if prev.retry_after_ms:
            time.sleep(prev.retry_after_ms / 1000)
            continue
        if done:
            break
        if realtime:
            while received["n"] == n:     # wait for the next chunk
                time.sleep(poll_seconds)

    first = next((e for e in events if e["text"].strip()), None)
    windows = [e["asr"] for e in events]
    res = prev
    ref_text = " ".join(t["text"] for t in turns)
    hyp_text = " ".join(u.text for u in res.utterances)
    metrics = {
        "duration": round(duration, 2),
        "first_text_wall": None if first is None else round(first["wall"], 1),
        "first_text_audio_in": None if first is None else first["audio_in"],
        "windows": len(events),
        "window_asr_seconds": {"min": min(windows, default=0), "max": max(windows, default=0),
                               "mean": round(sum(windows) / max(1, len(windows)), 2)},
        "asr_total": round(sum(c["asr_seconds"] for c in calls), 1),
        "audio_per_asr_second": round(duration / max(1e-6, sum(c["asr_seconds"] for c in calls)), 2),
        "lag_max": max((round(e["audio_in"] - e["processed"], 1) for e in events), default=None),
        "wall_total": round(wall(), 1),
        "cer": round(cer(ref_text, hyp_text), 4) if turns else None,
        "per_turn": [{"lang": t["lang"], "ref": t["text"], "hyp": h, "cer": round(c, 3)}
                     for t in turns for c, h in [turn_cer(t["text"], hyp_text)]],
        "latency": phrase_latency(events, turns, speed if realtime else 1e9) if realtime else [],
    }
    return {"duration": round(duration, 2), "chunks": len(chunks), "calls": calls, "events": events,
            "first_text": None if first is None else {"wall": round(first["wall"], 1), "audio_received": first["audio_in"]},
            "container": str(container), "result": res, "metrics": metrics}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="+")
    ap.add_argument("--chunk-seconds", type=float, default=2.0)
    ap.add_argument("--every-seconds", type=float, default=4.0, help="non-realtime: audio fed per call")
    ap.add_argument("--realtime", action="store_true")
    ap.add_argument("--speed", type=float, default=1.0, help="realtime: audio seconds per wall second")
    ap.add_argument("--language", default=None, help="meeting language: auto|ru|kk (default ASR_LANGUAGE)")
    ap.add_argument("--final", action="store_true", help="run process_recording on the full container")
    ap.add_argument("--json")
    args = ap.parse_args()
    if not shutil.which("ffmpeg"):
        print("ffmpeg required")
        return 2
    r = simulate([Path(w) for w in args.wav], chunk_seconds=args.chunk_seconds, every_seconds=args.every_seconds,
                 realtime=args.realtime, speed=args.speed, language=args.language)
    for c in r["calls"]:
        print(f"chunk {c['chunk']:3d} audio={c['audio_received']:6.1f}s decoded={c['decoded']:6.1f}s "
              f"processed={c['processed_until']:6.1f}s lag={c['lag']:5.1f}s win={c['windows']} "
              f"asr={c['asr_seconds']:5.2f}s call={c['call_seconds']:5.2f}s {c['status']} "
              f"pending={c['pending']} final={c['final']}/{c['total']} {c['error'] or ''}")
    m = r["metrics"]
    print("\nMETRICS:", json.dumps({k: v for k, v in m.items() if k not in ("per_turn", "latency")}, ensure_ascii=False))
    for t in m["latency"]:
        print(f"  latency {t['lang']:5s} end={t['end']:6.1f}s first={t['first_s']}s final={t['final_s']}s  {t['text']}")
    print("\nPER TURN CER, best-matching span (your terminal only):")
    for t in m["per_turn"]:
        print(f"  {t['cer']:.3f} [{t['lang']}] {t['hyp']}")
    print("\nPREVIEW:")
    for u in r["result"].utterances:
        print(f"  {u.id} [{u.start:6.2f}-{u.end:6.2f}] {'F' if u.is_final else '~'} {u.text}")
    if args.json:
        Path(args.json).write_text(json.dumps({k: v for k, v in r.items() if k not in ("result", "events")} |
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
