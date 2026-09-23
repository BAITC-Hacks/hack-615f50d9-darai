"""Importable, synchronous AI pipeline: the only entry point the backend calls.

Contract: docs/AI_CONTRACT.md. No DB, no HTTP, no FastAPI. Heavy functions
block; the backend runs them outside the event loop, one job at a time.
Logs carry stage names, durations and counts only: never text, audio,
vectors or tokens.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np

from . import align as align_mod
from . import audio
from .ai_types import (
    AIError,
    EnrollmentResult,
    ExtractionOutcome,
    MeetingContext,
    ProcessRequest,
    ProcessResult,
    SpeakerResult,
    Stage,
    StageTiming,
    TranscriptLine,
    UtteranceResult,
    VoiceModelInfo,
    VoiceQualityRejected,
)
from .asr import env_bool, transcribe
from .config import Settings, get_settings
from .diarize import DiarizationResult, diarize
from .extract import run_extraction
from .llm import LLMClient
from .ml import ModelRegistry, ModelUnavailable, get_registry
from .voice import (
    clean_segments,
    embed_regions,
    energy_speech_regions,
    enroll_vector,
    enrollment_quality,
    match_speakers,
    speaker_vectors,
)

log = logging.getLogger("darai.ai")
T = TypeVar("T")

NORMALIZED_NAME = "normalized.wav"


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


class AIPipeline:
    def __init__(self, settings: Settings | None = None, registry: ModelRegistry | None = None,
                 llm_client: LLMClient | None = None):
        self.settings = settings or get_settings()
        self.registry = registry or get_registry()
        self.llm = llm_client or LLMClient(self.settings)

    # ------------------------------------------------------------ status

    def voice_model_info(self) -> VoiceModelInfo:
        """Current profile model without loading weights."""
        s = self.settings
        revision = s.voice_model_revision
        rev_file = s.resolved_embedding_path / "REVISION"
        if revision == "local" and rev_file.is_file():
            # written by scripts/ai/prepare_models.py: "<repo>@<sha>"
            revision = rev_file.read_text().strip().rpartition("@")[2] or revision
        return VoiceModelInfo(model_id=s.voice_model_id, revision=revision, dimension=s.voice_embedding_dim)

    def models_status(self) -> dict[str, dict]:
        return self.registry.status()

    def preload(self) -> None:
        for key in ("embedding", "diarization", "asr"):
            try:
                self.registry.get(key)
            except ModelUnavailable:
                pass  # reflected in models_status()

    # ------------------------------------------------------------ helpers

    def _normalize(self, source_path: Path, work_dir: Path, name: str) -> tuple[Path, np.ndarray]:
        src = Path(source_path).resolve()
        wd = Path(work_dir).resolve()
        if not src.is_file():
            raise AIError("AUDIO_INVALID", "Исходный файл не найден")
        wd.mkdir(parents=True, exist_ok=True)
        dst = wd / name
        if dst == src:
            raise AIError("INTERNAL_ERROR", "Исходный файл совпадает с целевым")
        try:
            duration = audio.probe_duration(src, timeout=min(60, self.settings.ffmpeg_timeout_seconds))
            if duration > self.settings.max_audio_seconds:
                raise AIError("AUDIO_TOO_LONG", f"Запись длиннее {self.settings.max_audio_seconds} с")
            audio.normalize(src, dst, timeout=self.settings.ffmpeg_timeout_seconds)
            samples = audio.load_wav(dst)
        except audio.AudioError as exc:
            raise AIError(exc.code, exc.message)
        seconds = samples.size / audio.SAMPLE_RATE
        if seconds > self.settings.max_audio_seconds:
            raise AIError("AUDIO_TOO_LONG", f"Запись длиннее {self.settings.max_audio_seconds} с")
        if seconds < 0.5 or float(np.max(np.abs(samples), initial=0.0)) < 1e-3:
            raise AIError("AUDIO_EMPTY", "В записи нет звука")
        return dst, samples

    def _model(self, key: str, stage: Stage):
        try:
            return self.registry.get(key)
        except ModelUnavailable as exc:
            raise AIError("MODEL_UNAVAILABLE", exc.message, stage=stage, model=exc.model)

    def _embed_dim_check(self, vec: np.ndarray, stage: Stage | None) -> None:
        if vec.shape[-1] != self.settings.voice_embedding_dim:
            raise AIError("EMBEDDING_FAILED",
                          f"Размерность эмбеддинга {vec.shape[-1]} ≠ VOICE_EMBEDDING_DIM "
                          f"{self.settings.voice_embedding_dim}", stage=stage)

    # ------------------------------------------------------------ enrollment

    def enroll_voice(self, source_path: Path, work_dir: Path) -> EnrollmentResult:
        s = self.settings
        wav, samples = None, None
        try:
            try:
                wav, samples = self._normalize(source_path, work_dir, "enroll.wav")
            except AIError as exc:
                if exc.code == "AUDIO_EMPTY":
                    raise VoiceQualityRejected(["silence"], 0.0, s.voice_enroll_min_seconds,
                                               "В образце нет речи")
                raise
            regions = energy_speech_regions(samples, audio.SAMPLE_RATE)
            q = enrollment_quality(samples, audio.SAMPLE_RATE, regions, s.voice_enroll_min_seconds,
                                   _env_float("VOICE_CLIPPING_REVIEW_RATIO", 0.001),
                                   _env_float("VOICE_CLIPPING_REJECT_RATIO", 0.02))
            if q.reject_reasons:
                raise VoiceQualityRejected(q.reject_reasons, q.speech_seconds, s.voice_enroll_min_seconds,
                                           _reject_message(q.reject_reasons, q.speech_seconds,
                                                           s.voice_enroll_min_seconds))
            inference = self._model("embedding", None)
            try:
                vectors = embed_regions(inference, _torch_waveform(samples), audio.SAMPLE_RATE, regions,
                                        s.voice_enroll_chunk_seconds, s.voice_min_segment_seconds,
                                        s.voice_max_segments_per_speaker)
            except Exception as exc:
                log.error("enrollment embedding failed: %s", type(exc).__name__)
                raise AIError("EMBEDDING_FAILED", "Не удалось вычислить голосовой вектор")
            if not vectors:
                raise VoiceQualityRejected(["too_short"], q.speech_seconds, s.voice_enroll_min_seconds,
                                           "Нет фрагментов чистой речи достаточной длины")
            profile, consistency, reasons = enroll_vector(vectors, s.voice_enroll_consistency_threshold)
            self._embed_dim_check(profile, None)
            reasons = q.review_reasons + reasons
            log.info("enrollment done: chunks=%d speech=%.1fs status=%s", len(vectors), q.speech_seconds,
                     "needs_review" if reasons else "ok")
            return EnrollmentResult(
                vector=[float(x) for x in profile], model=self.voice_model_info(),
                quality_status="needs_review" if reasons else "ok", reasons=reasons,
                speech_seconds=q.speech_seconds, chunks=len(vectors), consistency=consistency,
            )
        finally:
            if wav is not None:
                Path(wav).unlink(missing_ok=True)

    # ------------------------------------------------------------ recording

    def process_recording(self, req: ProcessRequest) -> ProcessResult:
        s = self.settings
        timings: list[StageTiming] = []

        def run(stage: Stage, code: str, fn: Callable[[], T]) -> T:
            if req.is_cancelled():
                raise AIError("CANCELLED", "Обработка отменена", stage=stage)
            req.on_stage(stage)
            t0 = time.perf_counter()
            try:
                return fn()
            except AIError as exc:
                exc.stage = exc.stage or stage
                raise
            except ModelUnavailable as exc:
                raise AIError("MODEL_UNAVAILABLE", exc.message, stage=stage, model=exc.model)
            except Exception as exc:  # message may contain content: type only
                log.error("stage %s failed: %s", stage, type(exc).__name__)
                raise AIError(code, f"Сбой на этапе {stage}: {type(exc).__name__}", stage=stage)
            finally:
                dt = time.perf_counter() - t0
                timings.append(StageTiming(stage, round(dt, 2)))
                log.info("stage %s: %.2fs", stage, dt)

        wav, samples = run("normalizing", "FFMPEG_FAILED",
                           lambda: self._normalize(req.source_path, req.work_dir, NORMALIZED_NAME))
        duration = samples.size / audio.SAMPLE_RATE

        def _asr():
            model = self._model("asr", "transcribing")
            refine = env_bool("ASR_REFINE_LANGUAGES", True)
            allowed = [x.strip() for x in (os.environ.get("ASR_ALLOWED_LANGUAGES") or "ru,kk,en").split(",")
                       if x.strip()]
            return transcribe(model, samples, language=s.asr_language, beam_size=s.asr_beam_size,
                              multilingual=env_bool("ASR_MULTILINGUAL", True),
                              vad_filter=env_bool("ASR_VAD_FILTER", True), is_cancelled=req.is_cancelled,
                              allowed_languages=allowed if refine else None,
                              min_language_prob=_env_float("ASR_LANGUAGE_MIN_PROB", 0.5))

        asr = run("transcribing", "ASR_FAILED", _asr)
        if req.is_cancelled():
            raise AIError("CANCELLED", "Обработка отменена", stage="transcribing")
        words = asr.words
        if not words:
            raise AIError("AUDIO_EMPTY", "Речь в записи не распознана", stage="transcribing")

        def _diar() -> DiarizationResult:
            pipeline = self._model("diarization", "diarizing")
            return diarize(pipeline, samples, audio.SAMPLE_RATE,
                           max_speakers=_env_int("DIARIZATION_MAX_SPEAKERS"))

        diar = run("diarizing", "DIARIZATION_FAILED", _diar)

        def _align() -> list[UtteranceResult]:
            aligned = align_mod.align(words, diar.turns, diar.overlaps, max_gap=s.align_max_gap_seconds,
                                      ambiguity_ratio=s.align_ambiguity_ratio)
            return [UtteranceResult(index=i, start=u.start, end=u.end, speaker_label=u.speaker_label,
                                    text=u.text, language=asr.language_at((u.start + u.end) / 2),
                                    uncertain_reasons=list(u.uncertain_reasons))
                    for i, u in enumerate(aligned)]

        utterances = run("aligning", "INTERNAL_ERROR", _align)
        speakers = run("identifying", "EMBEDDING_FAILED",
                       lambda: self._identify(samples, diar, utterances, req))

        extraction = ExtractionOutcome(status="not_started")
        if req.run_extraction:
            by_label = {sp.label: sp for sp in speakers}
            lines = [TranscriptLine(id=u.index, speaker_label=u.speaker_label,
                                    employee_id=_bound_employee(by_label.get(u.speaker_label or "")),
                                    text=u.text, start=u.start) for u in utterances]
            extraction = run("extracting", "INTERNAL_ERROR", lambda: self.extract(lines, req.meeting))

        log.info("recording processed: %.1fs audio, %d utterances, %d speakers, extraction=%s",
                 duration, len(utterances), len(speakers), extraction.status)
        return ProcessResult(normalized_path=wav, duration_seconds=round(duration, 3),
                             languages=asr.languages, speakers=speakers, utterances=utterances,
                             extraction=extraction, timings=timings)

    def _identify(self, samples: np.ndarray, diar: DiarizationResult, utterances: list[UtteranceResult],
                  req: ProcessRequest) -> list[SpeakerResult]:
        s = self.settings
        labels = sorted(set(diar.labels) | {u.speaker_label for u in utterances if u.speaker_label})
        clean = clean_segments(diar.turns, diar.overlaps, s.voice_min_segment_seconds)
        for label in labels:
            clean.setdefault(label, [])
        dim = s.voice_embedding_dim
        candidates = {c.employee_id: np.asarray(c.vector, dtype=np.float64)
                      for c in req.candidates if len(c.vector) == dim}
        if len(candidates) != len(req.candidates):
            log.warning("skipped %d candidate(s) with incompatible dimension",
                        len(req.candidates) - len(candidates))
        vectors: dict[str, np.ndarray | None]
        if candidates:
            inference = self._model("embedding", "identifying")
            waveform = _torch_waveform(samples)
            vectors = speaker_vectors(inference, waveform, audio.SAMPLE_RATE, clean,
                                      s.voice_enroll_chunk_seconds, s.voice_min_segment_seconds,
                                      s.voice_max_segments_per_speaker)
            for v in vectors.values():
                if v is not None:
                    self._embed_dim_check(v, "identifying")
        else:
            # Nothing to compare with: skip embedding work, report why.
            vectors = {label: (np.ones(dim) if clean[label] else None) for label in labels}
        matches = match_speakers({lb: vectors.get(lb) for lb in labels}, candidates,
                                 s.voice_match_threshold, s.voice_match_margin)
        out = []
        for label in labels:
            m = matches[label]
            out.append(SpeakerResult(
                label=label, proposed_employee_id=m.proposed_employee_id, similarity=m.similarity,
                second_similarity=m.second_similarity, review_required=m.review_required,
                review_reasons=list(m.review_reasons),
                clean_speech_seconds=round(sum(e - b for b, e in clean[label]), 2),
                speech_seconds=round(diar.speech_seconds(label), 2),
            ))
        return out

    # ------------------------------------------------------------ extraction

    def extract(self, lines: list[TranscriptLine], meeting: MeetingContext) -> ExtractionOutcome:
        """Tasks + summary. LLM failures return status="error", never raise."""
        return run_extraction(self.llm, lines, list(meeting.participants), meeting.starts_at_local,
                              meeting.timezone, self.settings)


def _bound_employee(sp: SpeakerResult | None):
    return sp.proposed_employee_id if sp is not None and not sp.review_required else None


def _torch_waveform(samples: np.ndarray):
    import torch

    return torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32)).unsqueeze(0)


def _reject_message(reasons: list[str], speech: float, required: float) -> str:
    if "silence" in reasons:
        return "В образце нет речи"
    if "too_short" in reasons:
        return f"Слишком мало чистой речи: {speech:.1f} с, нужно не менее {required:.0f} с"
    if "clipping" in reasons:
        return "Сильные искажения (перегруз) в образце, запишите тише"
    return "Образец не прошёл проверку качества"


# ---------------------------------------------------------------- module API

_pipeline: AIPipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> AIPipeline:
    global _pipeline
    with _pipeline_lock:
        if _pipeline is None:
            _pipeline = AIPipeline()
        return _pipeline


def set_pipeline(p: AIPipeline | None) -> None:
    """Tests only."""
    global _pipeline
    with _pipeline_lock:
        _pipeline = p


def process_recording(req: ProcessRequest) -> ProcessResult:
    return get_pipeline().process_recording(req)


def extract(lines: list[TranscriptLine], meeting: MeetingContext) -> ExtractionOutcome:
    return get_pipeline().extract(lines, meeting)


def enroll_voice(source_path: Path, work_dir: Path) -> EnrollmentResult:
    return get_pipeline().enroll_voice(source_path, work_dir)


def voice_model_info() -> VoiceModelInfo:
    return get_pipeline().voice_model_info()


def models_status() -> dict[str, dict]:
    return get_pipeline().models_status()


def preload() -> None:
    if get_pipeline().settings.preload_models:
        get_pipeline().preload()
