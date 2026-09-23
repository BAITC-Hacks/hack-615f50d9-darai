"""Incremental transcript preview for a live MediaRecorder session.

Each call:
1. decodes the fixed snapshot of the accumulated container with FFmpeg (a
   truncated last cluster is expected and not an error);
2. transcribes only new audio, sequentially, in windows of
   LIVE_PREVIEW_WINDOW_SECONDS starting LIVE_PREVIEW_OVERLAP_SECONDS before the
   committed position, with the already loaded faster-whisper model;
3. merges words by TIME: a word is new only if its midpoint lies after the
   committed position, so the overlap is not duplicated while the same word
   spoken later is kept;
4. commits words that are safely inside the window; words near the live edge
   stay tentative and are re-decoded by the next call.

No diarization, no LLM, no DB: the protocol comes from
ai_pipeline.process_recording on the full file after the session ends.
Timing is reported honestly through processed_until_seconds/asr_seconds.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

from . import audio
from .ai_types import AIError
from .asr import env_bool, transcribe
from .config import get_settings
from .live_ai_types import (
    LivePreviewRequest,
    LivePreviewResult,
    LivePreviewState,
    LivePreviewUtterance,
    PreviewWord,
)
from .ml import ModelUnavailable, compute_lock, get_registry
from .voice import energy_speech_regions

log = logging.getLogger("darai.live")

PREVIEW_WAV = "preview.wav"
_SENTENCE_END = (".", "!", "?", "…")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


class LiveConfig:
    def __init__(self):
        self.window = _env_float("LIVE_PREVIEW_WINDOW_SECONDS", 24.0)  # measured: 12 s cannot keep up on CPU
        self.overlap = _env_float("LIVE_PREVIEW_OVERLAP_SECONDS", 1.5)
        self.guard = _env_float("LIVE_PREVIEW_GUARD_SECONDS", 1.5)       # live-edge words stay tentative
        self.min_new = _env_float("LIVE_PREVIEW_MIN_NEW_SECONDS", 3.0)   # below this: "waiting"
        self.max_windows = int(_env_float("LIVE_PREVIEW_MAX_WINDOWS_PER_CALL", 3))
        self.utterance_gap = _env_float("LIVE_PREVIEW_UTTERANCE_GAP_SECONDS", 1.0)
        self.max_utterance = _env_float("LIVE_PREVIEW_MAX_UTTERANCE_SECONDS", 20.0)
        self.no_speech_prob = _env_float("LIVE_PREVIEW_NO_SPEECH_PROB", 0.6)
        self.hallucination_silence = _env_float("LIVE_PREVIEW_HALLUCINATION_SILENCE_SECONDS", 1.0) or None
        self.beam_size = int(_env_float("LIVE_PREVIEW_BEAM_SIZE", 0)) or None   # default: ASR_BEAM_SIZE
        # Per-segment language refinement: measured lag up to 87 s on CPU M4 Pro -> off for preview;
        # the final pipeline keeps ASR_REFINE_LANGUAGES.
        self.refine = env_bool("LIVE_PREVIEW_REFINE_LANGUAGES", False)


# ------------------------------------------------------------------ decoding


def decode_snapshot(src: Path, dst: Path, timeout: int) -> np.ndarray:
    """Decode everything decodable in a (possibly truncated) container prefix."""
    tmp = dst.with_suffix(".part.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-err_detect", "ignore_err", "-i", str(src),
             "-vn", "-ac", "1", "-ar", str(audio.SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
            capture_output=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        raise AIError("FFMPEG_FAILED", "ffmpeg не установлен")
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise AIError("FFMPEG_FAILED", "FFmpeg превысил таймаут")
    # Non-zero exit on a truncated tail is normal: keep whatever was written.
    if not tmp.exists() or tmp.stat().st_size <= 44:
        tmp.unlink(missing_ok=True)
        return np.zeros(0, dtype=np.float32)  # e.g. header-only prefix
    tmp.replace(dst)
    try:
        return audio.load_wav(dst)
    except Exception:
        return np.zeros(0, dtype=np.float32)


# ------------------------------------------------------------------ merging (pure, tested)


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", "", text.lower())


def new_words_after(words: list[PreviewWord], committed_until: float,
                    recent: list[str], eps: float = 0.05) -> list[PreviewWord]:
    """Words of a window that are not already committed.

    Time decides: a word is new if it ENDS after the committed position (Whisper
    often stretches the start of the first word after a pause backwards). A word
    that straddles the position and repeats one of the last committed words is
    the overlap re-recognized, and is dropped. The same word spoken later starts
    after the position and is always kept.
    """
    recent_norm = {_norm(t) for t in recent}
    out = []
    for w in words:
        if w.end <= committed_until + eps:
            continue
        if not out and w.start < committed_until and _norm(w.text) in recent_norm:
            continue
        out.append(w)
    return out


def split_commit(words: list[PreviewWord], commit_limit: float) -> tuple[list[PreviewWord], list[PreviewWord]]:
    commit: list[PreviewWord] = []
    for i, w in enumerate(words):
        if w.end > commit_limit:
            return commit, words[i:]
        commit.append(w)
    return commit, []


def _text(words) -> str:
    return "".join(w.text for w in words).strip()


def close_utterances(state: LivePreviewState, open_words: list[PreviewWord], committed_until: float,
                     gap: float, max_len: float) -> tuple[list[LivePreviewUtterance], list[PreviewWord], int]:
    """Turn committed words into final utterances; the last one stays open until silence follows."""
    finals: list[LivePreviewUtterance] = []
    next_id = state.next_id
    cur: list[PreviewWord] = []

    def flush():
        nonlocal next_id
        if cur and _text(cur):
            finals.append(LivePreviewUtterance(f"preview-{next_id}", round(cur[0].start, 3),
                                               round(cur[-1].end, 3), None, _text(cur), True))
            next_id += 1
        cur.clear()

    for w in open_words:
        if cur:
            prev = cur[-1]
            pause = w.start - prev.end
            sentence = prev.text.strip().endswith(_SENTENCE_END)
            if pause > gap or (sentence and pause >= 0.25) or (sentence and w.start - cur[0].start > max_len):
                flush()
        cur.append(w)
    if cur:
        pause = committed_until - cur[-1].end
        if pause > gap or (cur[-1].text.strip().endswith(_SENTENCE_END) and pause >= 0.25):
            flush()  # a committed pause follows: the utterance is complete
    return finals, list(cur), next_id


def snapshot(state: LivePreviewState) -> list[LivePreviewUtterance]:
    out = list(state.final)
    nid = state.next_id
    if state.open_words and _text(state.open_words):
        w = state.open_words
        out.append(LivePreviewUtterance(f"preview-{nid}", round(w[0].start, 3), round(w[-1].end, 3), None,
                                        _text(w), False))
        nid += 1
    if state.tentative and _text(state.tentative):
        w = state.tentative
        out.append(LivePreviewUtterance(f"preview-{nid}", round(w[0].start, 3), round(w[-1].end, 3), None,
                                        _text(w), False))
    return out


def merge_window(state: LivePreviewState, window_words: list[PreviewWord], window_end: float,
                 live_edge: bool, cfg: LiveConfig,
                 voiced: list[tuple[float, float]] | None = None) -> LivePreviewState:
    """Pure state transition for one transcribed window (absolute timestamps)."""
    recent = [w.text for w in state.open_words[-3:]]
    if len(recent) < 3 and state.final:
        recent = state.final[-1].text.split()[-(3 - len(recent)):] + recent
    fresh = new_words_after(window_words, state.committed_until, recent)
    commit_limit = window_end - (cfg.guard if live_edge else cfg.overlap)
    commit, rest = split_commit(fresh, commit_limit)
    # Never jump past speech the model may have missed at the window edge: commit up to
    # the last committed word; move past a pause only up to (limit - overlap), so the
    # next window (which starts `overlap` earlier) re-covers the edge.
    committed_until = state.committed_until
    if commit:
        committed_until = max(committed_until, commit[-1].end)
    if not rest:
        skip_to = commit_limit - cfg.overlap
        if voiced is not None:
            # Only a real pause may be skipped: voiced audio the model returned no words
            # for (edge of window, hard Kazakh phrase) stays uncommitted for the next window.
            after = committed_until
            for v0, v1 in voiced:
                if v1 > after + 0.3:
                    skip_to = min(skip_to, max(v0, after))
                    break
        committed_until = max(committed_until, skip_to)
    if not live_edge and committed_until <= state.committed_until + 0.1:
        # A word straddling the limit of a backlog window must not stall progress forever.
        commit, rest = fresh, []
        committed_until = max(commit_limit - cfg.overlap, commit[-1].end if commit else 0.0,
                              state.committed_until + 0.5)
    finals, open_words, next_id = close_utterances(
        state, list(state.open_words) + commit, committed_until, cfg.utterance_gap, cfg.max_utterance)
    return LivePreviewState(committed_until=round(committed_until, 3), next_id=next_id,
                            final=state.final + tuple(finals), open_words=tuple(open_words),
                            tentative=tuple(rest) if live_edge else ())


# ------------------------------------------------------------------ entry point


def quiet_point(samples: np.ndarray, around: float, radius: float = 0.75) -> float:
    """Lowest-energy 20 ms frame near ``around``: windows start in a pause, not inside a word
    (a window cut mid-word made Whisper emit a caption hallucination for the whole window)."""
    sr = audio.SAMPLE_RATE
    lo, hi = max(0, int((around - radius) * sr)), min(samples.size, int((around + radius) * sr))
    frame = int(0.02 * sr)
    if hi - lo < frame * 2:
        return max(0.0, around)
    seg = samples[lo:lo + (hi - lo) // frame * frame].reshape(-1, frame)
    rms = np.sqrt(np.mean(seg.astype(np.float64) ** 2, axis=1))
    return (lo + int(np.argmin(rms)) * frame) / sr


def _words_of(res, offset: float, max_no_speech: float) -> list[PreviewWord]:
    out = []
    for seg in res.segments:
        # VAD already drops silence; this also drops Whisper's typical silence hallucinations.
        if seg.no_speech_prob is not None and seg.no_speech_prob > max_no_speech:
            continue
        out.extend(PreviewWord(round(offset + w.start, 3), round(offset + w.end, 3), w.text) for w in seg.words)
    return out


def _allowed() -> list[str]:
    return [x.strip() for x in (os.environ.get("ASR_ALLOWED_LANGUAGES") or "ru,kk,en").split(",") if x.strip()]


def transcribe_preview(req: LivePreviewRequest, *, registry=None) -> LivePreviewResult:
    """Never raises for model/audio problems: returns preview_status="unavailable" with the
    previous utterances kept. Raises AIError("CANCELLED") only when is_cancelled() is True."""
    prev = req.previous
    state = prev.state if prev else LivePreviewState()

    def keep(status, error=None, decoded=0.0) -> LivePreviewResult:
        return LivePreviewResult(processed_until_seconds=prev.processed_until_seconds if prev else 0.0,
                                 utterances=snapshot(state), preview_status=status, preview_error=error,
                                 state=state, decoded_seconds=decoded or (prev.decoded_seconds if prev else 0.0))

    if not compute_lock.acquire(blocking=False):
        return keep("waiting")  # full processing (or another preview) is running
    try:
        return _run(req, state, prev, keep, registry)
    except AIError as exc:
        if exc.code == "CANCELLED":
            raise
        return keep("unavailable", {"code": exc.code, "message": exc.message})
    except Exception as exc:
        log.error("preview failed: %s", type(exc).__name__)
        return keep("unavailable", {"code": "ASR_FAILED", "message": f"Сбой предпросмотра: {type(exc).__name__}"})
    finally:
        compute_lock.release()


def _run(req: LivePreviewRequest, state: LivePreviewState, prev, keep, registry) -> LivePreviewResult:
    s = get_settings()
    cfg = LiveConfig()
    cancelled = req.is_cancelled

    def check():
        if cancelled():
            raise AIError("CANCELLED", "Предпросмотр отменён")

    check()
    src = Path(req.container_path)
    if not src.is_file():
        raise AIError("AUDIO_INVALID", "Снимок записи не найден")
    work = Path(req.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    samples = decode_snapshot(src, work / PREVIEW_WAV, timeout=min(120, s.ffmpeg_timeout_seconds))
    total = samples.size / audio.SAMPLE_RATE
    processed = prev.processed_until_seconds if prev else 0.0
    if total - processed < cfg.min_new:
        return keep("waiting", decoded=total)

    check()
    reg = registry or get_registry()
    try:
        model = reg.get("asr")
    except ModelUnavailable as exc:
        raise AIError("MODEL_UNAVAILABLE", exc.message, model=exc.model)

    asr_time = 0.0
    for _ in range(max(1, cfg.max_windows)):
        start = 0.0 if state.committed_until <= 0 else quiet_point(samples, state.committed_until - cfg.overlap)
        start = min(start, state.committed_until)  # never skip uncommitted audio
        end = min(total, start + cfg.window)
        if end - start < 0.5:
            break
        live_edge = end >= total
        clip = samples[int(start * audio.SAMPLE_RATE):int(end * audio.SAMPLE_RATE)]
        check()
        t0 = time.perf_counter()
        language = req.asr_language if req.asr_language is not None else s.asr_language
        res = transcribe(model, clip, language=None if language == "auto" else language,
                         profile=req.asr_profile or s.asr_profile, beam_size=cfg.beam_size or s.asr_beam_size,
                         multilingual=env_bool("ASR_MULTILINGUAL", True),
                         vad_filter=env_bool("ASR_VAD_FILTER", True), is_cancelled=cancelled,
                         allowed_languages=_allowed() if cfg.refine else None,
                         min_language_prob=_env_float("ASR_LANGUAGE_MIN_PROB", 0.5),
                         hallucination_silence_threshold=cfg.hallucination_silence)
        dt = time.perf_counter() - t0
        asr_time += dt
        check()
        voiced = [(start + a, start + b) for a, b in energy_speech_regions(clip, audio.SAMPLE_RATE)]
        state = merge_window(state, _words_of(res, start, cfg.no_speech_prob), end, live_edge, cfg, voiced)
        processed = end
        log.info("preview window %.1f-%.1fs: asr %.2fs (x%.2f)", start, end, dt, dt / max(end - start, 1e-6))
        if live_edge:
            break
    return LivePreviewResult(processed_until_seconds=round(processed, 3), utterances=snapshot(state),
                             preview_status="ready", preview_error=None, state=state,
                             decoded_seconds=round(total, 3), asr_seconds=round(asr_time, 2))
