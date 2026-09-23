"""Incremental transcript preview for a live MediaRecorder session.

Each call:
1. decodes the fixed snapshot (first ``stable_bytes``) of the accumulated container
   with FFmpeg (a truncated last cluster is expected and not an error);
2. transcribes only audio after the committed position, sequentially, in windows of
   LIVE_PREVIEW_WINDOW_SECONDS starting LIVE_PREVIEW_CONTEXT_SECONDS before it (in a
   pause), with the live model ("live_asr" if LIVE_ASR_MODEL_PATH is set, else "asr");
   auto language = one encoder pass per window, language restricted to
   ASR_ALLOWED_LANGUAGES; voiced audio the window decode dropped is re-decoded alone;
3. merges words by TIME: a word is new only if it ends after the committed position,
   so the overlap is not duplicated while the same word spoken later is kept;
4. commits words that are safely inside the window; words near the live edge stay
   tentative and are re-decoded by the next window;
5. publishes a full snapshot through ``on_update`` after EVERY window and reports
   ``has_pending_audio`` when the same snapshot still holds backlog.

No diarization, no LLM, no DB: the protocol comes from
ai_pipeline.process_recording (large-v3) on the full file after the session ends.
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
from .asr import env_bool, pick_language, transcribe
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
        # Measured (docs/AI_SETUP.md §10): a window costs ~constant time (Whisper encodes a 30 s block),
        # so the window only caps backlog; 12 s + per-window publication keeps up with turbo on CPU.
        self.window = _env_float("LIVE_PREVIEW_WINDOW_SECONDS", 12.0)
        self.overlap = _env_float("LIVE_PREVIEW_OVERLAP_SECONDS", 1.5)   # backlog window: tail left uncommitted
        self.context = _env_float("LIVE_PREVIEW_CONTEXT_SECONDS", 0.5)   # audio before the committed point
        self.guard = _env_float("LIVE_PREVIEW_GUARD_SECONDS", 1.5)       # live-edge words stay tentative
        self.min_new = _env_float("LIVE_PREVIEW_MIN_NEW_SECONDS", 3.0)   # below this: "waiting"
        self.max_windows = int(_env_float("LIVE_PREVIEW_MAX_WINDOWS_PER_CALL", 4))  # each one is published
        self.utterance_gap = _env_float("LIVE_PREVIEW_UTTERANCE_GAP_SECONDS", 1.0)
        self.max_utterance = _env_float("LIVE_PREVIEW_MAX_UTTERANCE_SECONDS", 20.0)
        self.no_speech_prob = _env_float("LIVE_PREVIEW_NO_SPEECH_PROB", 0.6)
        self.hallucination_silence = _env_float("LIVE_PREVIEW_HALLUCINATION_SILENCE_SECONDS", 1.0) or None
        self.beam_size = int(_env_float("LIVE_PREVIEW_BEAM_SIZE", 0)) or None   # default: ASR_BEAM_SIZE
        # Per-segment language refinement: measured lag up to 87 s on CPU M4 Pro -> off for preview;
        # the final pipeline keeps ASR_REFINE_LANGUAGES.
        self.refine = env_bool("LIVE_PREVIEW_REFINE_LANGUAGES", False)
        # auto language: pick the window language among ASR_ALLOWED_LANGUAGES (same cost as Whisper's own detection)
        self.restrict_languages = env_bool("LIVE_PREVIEW_RESTRICT_LANGUAGES", True)
        # voiced audio without words is re-decoded on its own (own language in auto mode)
        self.hole_min = _env_float("LIVE_PREVIEW_HOLE_MIN_SECONDS", 0.8)
        self.max_holes = int(_env_float("LIVE_PREVIEW_MAX_HOLES_PER_WINDOW", 2))
        self.busy_retry_ms = int(_env_float("LIVE_PREVIEW_BUSY_RETRY_MS", 1000))


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


def find_holes(words: list[PreviewWord], voiced: list[tuple[float, float]], lo: float, hi: float,
               min_len: float) -> list[tuple[float, float]]:
    """Voiced regions inside [lo, hi] (absolute seconds) for which the model returned (almost) no
    words: e.g. a Kazakh phrase inside a window decoded as Russian is silently dropped by Whisper."""
    holes = []
    for a, b in voiced:
        a, b = max(a, lo), min(b, hi)
        if b - a < min_len:
            continue
        covered = sum(max(0.0, min(b, w.end) - max(a, w.start)) for w in words)
        if covered < 0.25 * (b - a):
            holes.append((a, b))
    return holes


def _allowed() -> list[str]:
    return [x.strip() for x in (os.environ.get("ASR_ALLOWED_LANGUAGES") or "ru,kk,en").split(",") if x.strip()]


class _CallbackFailed(Exception):
    """on_update raised: re-raised unchanged by transcribe_preview, never reported as an ASR error."""

    def __init__(self, exc: BaseException):
        super().__init__(type(exc).__name__)
        self.exc = exc


def restore_state(prev: LivePreviewResult | None) -> LivePreviewState:
    """State for the next call. An old/lost state with a published snapshot (rows written
    before ``state`` existed) continues after the last FINAL utterance instead of
    re-transcribing the recording from zero; non-final text is re-decoded."""
    if prev is None:
        return LivePreviewState()
    st = prev.state
    if st.committed_until > 0 or st.final or st.open_words or st.tentative:
        return st
    finals = tuple(u for u in prev.utterances if u.is_final)
    if not finals:
        return st
    ids = [int(u.id.rsplit("-", 1)[-1]) for u in finals if u.id.rsplit("-", 1)[-1].isdigit()]
    return LivePreviewState(committed_until=float(finals[-1].end),
                            next_id=(max(ids) + 1) if ids else len(finals), final=finals)


def live_model_key() -> str:
    """"live_asr" when LIVE_ASR_MODEL_PATH is set (missing weights -> MODEL_UNAVAILABLE, no
    fallback), otherwise the already loaded final model "asr"."""
    return "live_asr" if (os.environ.get("LIVE_ASR_MODEL_PATH") or "").strip() else "asr"


def window_language(model, clip: np.ndarray, allowed: list[str]) -> str | None:
    """Best language among ``allowed`` for one window via the public API (extra encoder pass)."""
    try:
        _, _, probs = model.detect_language(clip, vad_filter=True)
    except Exception as exc:  # e.g. no speech left after VAD
        log.info("preview language detection skipped: %s", type(exc).__name__)
        return None
    lang, _ = pick_language(probs or [], allowed)
    return lang


class WindowEncoder:
    """One encoder pass per window in auto-language mode.

    faster-whisper's own auto mode encodes a window twice (language detection, then
    decoding), and on CPU the encoder dominates (30 s block whatever the window length).
    Here the window is encoded once, the language is picked among ASR_ALLOWED_LANGUAGES
    from that output, and the following ``model.transcribe`` gets the same encoder output
    for the identical first 30 s segment (exact array match; any other input is encoded
    normally, so a mismatch only costs time, never correctness). Patched per call under
    ``compute_lock`` and always restored.

    Also reports ``speech=False`` when Silero VAD (the one transcribe would use) finds no
    speech: the window is then skipped without an ASR call.
    """

    def __init__(self, model, clip: np.ndarray, *, vad: bool, detect: bool, allowed: list[str]):
        self.model, self.language, self.speech, self.reused = model, None, True, 0
        self._patched = False
        fe, inner = getattr(model, "feature_extractor", None), getattr(model, "model", None)
        if fe is None or inner is None or not hasattr(model, "encode"):   # fakes / other runtimes
            if detect:
                self.language = window_language(model, clip, allowed)
            return
        from faster_whisper.audio import pad_or_trim

        audio_ = clip
        if vad:
            from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps

            chunks = get_speech_timestamps(clip, VadOptions())
            if not chunks:
                self.speech = False
                return
            parts, _ = collect_chunks(clip, chunks)
            audio_ = np.concatenate(parts, axis=0)
        if not detect:
            return
        feats = fe(audio_)
        seg = pad_or_trim(feats[:, :min(fe.nb_max_frames, feats.shape[-1] - 1)])
        enc = model.encode(seg)
        probs = [(tok[2:-2], p) for tok, p in inner.detect_language(enc)[0]]
        self.language, _ = pick_language(probs, allowed)
        seg3, orig = seg[None], model.encode

        def encode(features):
            f = features if features.ndim == 3 else features[None]
            if f.shape == seg3.shape and np.array_equal(f, seg3):
                self.reused += 1
                return enc
            return orig(features)

        model.encode = encode      # instance attribute shadows the method for this window only
        self._patched = True

    def close(self):
        if self._patched:
            del self.model.encode
            self._patched = False


def transcribe_preview(req: LivePreviewRequest, *, registry=None) -> LivePreviewResult:
    """Transcribe new audio of a fixed snapshot window by window.

    After EVERY window ``req.on_update`` (if given) receives the full snapshot; the last one is
    returned. Never raises for model/audio problems: returns preview_status="unavailable" with
    the latest utterances kept. Raises AIError("CANCELLED") when is_cancelled() is True, and
    re-raises exceptions of on_update unchanged."""
    cfg = LiveConfig()
    prev = req.previous
    cur = {"state": restore_state(prev),
           "processed": prev.processed_until_seconds if prev else 0.0,
           "decoded": prev.decoded_seconds if prev else 0.0,
           "model": None}

    def keep(status, error=None, *, retry_after_ms=None) -> LivePreviewResult:
        return LivePreviewResult(processed_until_seconds=round(cur["processed"], 3),
                                 utterances=snapshot(cur["state"]), preview_status=status, preview_error=error,
                                 state=cur["state"], decoded_seconds=round(cur["decoded"], 3),
                                 retry_after_ms=retry_after_ms, asr_model=cur["model"])

    if not compute_lock.acquire(blocking=False):
        # the model is busy (final processing or another preview): retry, not "no audio"
        return keep("waiting", retry_after_ms=cfg.busy_retry_ms)
    try:
        return _run(req, cfg, cur, keep, registry)
    except _CallbackFailed as exc:
        raise exc.exc
    except AIError as exc:
        if exc.code == "CANCELLED":
            raise
        return keep("unavailable", {"code": exc.code, "message": exc.message})
    except Exception as exc:
        log.error("preview failed: %s", type(exc).__name__)
        return keep("unavailable", {"code": "ASR_FAILED", "message": f"Сбой предпросмотра: {type(exc).__name__}"})
    finally:
        compute_lock.release()


def _stable_source(req: LivePreviewRequest, work: Path) -> Path:
    """Only the first ``stable_bytes`` of the container are read, even if the file is longer."""
    src = Path(req.container_path)
    if not src.is_file():
        raise AIError("AUDIO_INVALID", "Снимок записи не найден")
    size = src.stat().st_size
    if 0 < req.stable_bytes < size:
        dst = work / f"stable{src.suffix or '.bin'}"
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            fo.write(fi.read(req.stable_bytes))
        return dst
    return src


def _run(req: LivePreviewRequest, cfg: LiveConfig, cur: dict, keep, registry) -> LivePreviewResult:
    s = get_settings()
    cancelled = req.is_cancelled

    def check():
        if cancelled():
            raise AIError("CANCELLED", "Предпросмотр отменён")

    check()
    work = Path(req.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    src = _stable_source(req, work)
    samples = decode_snapshot(src, work / PREVIEW_WAV, timeout=min(120, s.ffmpeg_timeout_seconds))
    total = samples.size / audio.SAMPLE_RATE
    if total + 1.0 < cur["decoded"]:
        # not a prefix of the same container (e.g. a single chunk passed as a file)
        return keep("unavailable", {"code": "AUDIO_INVALID",
                                    "message": "Снимок короче уже декодированного: нужен префикс того же контейнера"})
    cur["decoded"] = total
    if total - cur["processed"] < cfg.min_new:
        return keep("waiting")  # not enough new audio; has_pending_audio=False, no retry_after_ms

    check()
    reg = registry or get_registry()
    key = live_model_key()
    try:
        model = reg.get(key)
    except ModelUnavailable as exc:
        raise AIError("MODEL_UNAVAILABLE", exc.message, model=exc.model)
    cur["model"] = key

    language = req.asr_language if req.asr_language is not None else s.asr_language
    language = None if language in (None, "", "auto") else language   # meeting choice is never overridden
    profile = req.asr_profile or s.asr_profile
    allowed = _allowed()
    asr_time = 0.0
    last = None
    for _ in range(max(1, cfg.max_windows)):
        state = cur["state"]
        # The previous window already kept `overlap` (backlog) / `guard` (live edge) uncommitted
        # before its end; the next one starts only `context` before the committed point, in a pause.
        start = 0.0 if state.committed_until <= 0 else quiet_point(samples, state.committed_until - cfg.context,
                                                                   radius=min(0.75, cfg.context + 0.25))
        start = min(start, state.committed_until)  # never skip uncommitted audio
        end = min(total, start + cfg.window)
        if total - end < cfg.min_new and total - start <= 29.5:
            end = total   # do not leave a stub shorter than min_new behind the live edge
        if end - start < 0.5:
            break
        live_edge = end >= total
        clip = samples[int(start * audio.SAMPLE_RATE):int(end * audio.SAMPLE_RATE)]
        check()
        t0 = time.perf_counter()
        vad = env_bool("ASR_VAD_FILTER", True)
        enc = WindowEncoder(model, clip, vad=vad, allowed=allowed,
                            detect=language is None and cfg.restrict_languages and not cfg.refine)
        try:
            if not enc.speech:
                res = None   # VAD: no speech in the window, transcribe would return nothing
            else:
                res = transcribe(model, clip, language=language or enc.language, profile=profile,
                                 beam_size=cfg.beam_size or s.asr_beam_size,
                                 multilingual=env_bool("ASR_MULTILINGUAL", True),
                                 vad_filter=vad, is_cancelled=cancelled,
                                 allowed_languages=allowed if cfg.refine else None,
                                 min_language_prob=_env_float("ASR_LANGUAGE_MIN_PROB", 0.5),
                                 hallucination_silence_threshold=cfg.hallucination_silence)
        finally:
            enc.close()
        check()
        voiced = [(start + a, start + b) for a, b in energy_speech_regions(clip, audio.SAMPLE_RATE)]
        words = _words_of(res, start, cfg.no_speech_prob) if res is not None else []
        hi = end - (cfg.guard if live_edge else cfg.overlap)   # the tail is re-decoded by the next window anyway
        for a, b in find_holes(words, voiced, max(start, state.committed_until), hi, cfg.hole_min)[:cfg.max_holes]:
            check()
            a0, b0 = max(0.0, a - 0.2), min(total, b + 0.2)
            sub = samples[int(a0 * audio.SAMPLE_RATE):int(b0 * audio.SAMPLE_RATE)]
            henc = WindowEncoder(model, sub, vad=False, allowed=allowed, detect=language is None and not cfg.refine)
            try:
                hres = transcribe(model, sub, language=language or henc.language, profile=profile,
                                  beam_size=cfg.beam_size or s.asr_beam_size, multilingual=False,
                                  vad_filter=False, is_cancelled=cancelled,
                                  hallucination_silence_threshold=cfg.hallucination_silence) if henc.speech else None
            finally:
                henc.close()
            extra = [w for w in (_words_of(hres, a0, cfg.no_speech_prob) if hres else [])
                     if w.end > a - 0.1 and w.start < b + 0.1]
            log.info("preview hole %.1f-%.1fs: lang=%s words=%d", a, b, language or henc.language, len(extra))
            words = sorted(words + extra, key=lambda w: w.start)
        dt = time.perf_counter() - t0
        asr_time += dt
        cur["state"] = merge_window(state, words, end, live_edge, cfg, voiced)
        cur["processed"] = end
        log.info("preview window %.1f-%.1fs: asr %.2fs (x%.2f) lang=%s encoder_reused=%d speech=%s", start, end, dt,
                 dt / max(end - start, 1e-6), language or enc.language, enc.reused, enc.speech)
        last = LivePreviewResult(processed_until_seconds=round(end, 3), utterances=snapshot(cur["state"]),
                                 preview_status="ready", preview_error=None, state=cur["state"],
                                 decoded_seconds=round(total, 3), asr_seconds=round(asr_time, 2),
                                 has_pending_audio=total - end >= cfg.min_new, asr_model=key)
        if req.on_update is not None:
            try:
                req.on_update(last)
            except Exception as exc:
                raise _CallbackFailed(exc) from exc
        if live_edge:
            break
    return last or keep("waiting")
