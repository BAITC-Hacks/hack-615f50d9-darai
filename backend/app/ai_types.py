"""Typed boundary between the backend and the local AI pipeline.

No FastAPI / SQLAlchemy here: the backend converts these dataclasses to DB
rows and HTTP payloads. See docs/AI_CONTRACT.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Literal

Stage = Literal["normalizing", "transcribing", "diarizing", "aligning", "identifying", "extracting"]

SPEAKER_REVIEW_REASONS = (
    "no_clean_speech",        # < 1 clean (no overlap) segment >= VOICE_MIN_SEGMENT_SECONDS
    "no_candidates",          # nobody invited has a compatible voice profile
    "below_threshold",        # best similarity < VOICE_MATCH_THRESHOLD
    "low_margin",             # best - runner-up < VOICE_MATCH_MARGIN (2+ candidates only)
    "candidate_taken",        # best candidate went to another speaker with higher similarity
    "split_cluster_suspected",  # another speaker also confidently matched the same employee
)
UTTERANCE_UNCERTAIN_REASONS = ("no_speaker_overlap", "overlapped_speech", "ambiguous_speaker")
TASK_REVIEW_REASONS = (
    "missing_assignee", "missing_deadline", "deadline_without_source", "deadline_before_meeting",
    "low_confidence", "evidence_not_found", "unresolved_author",
)
AI_ERROR_CODES = (
    "AUDIO_INVALID", "AUDIO_EMPTY", "AUDIO_TOO_LONG", "FFMPEG_FAILED", "MODEL_UNAVAILABLE",
    "ASR_FAILED", "DIARIZATION_FAILED", "EMBEDDING_FAILED", "CANCELLED", "INTERNAL_ERROR",
)
EXTRACTION_ERROR_CODES = ("LLM_UNAVAILABLE", "LLM_TIMEOUT", "LLM_INVALID_RESPONSE", "LLM_FORBIDDEN_ENDPOINT")


@dataclass(frozen=True)
class Participant:
    employee_id: uuid.UUID
    fio: str
    position: str = ""


@dataclass(frozen=True)
class VoiceCandidate:
    """Invited participant's profile; backend already dropped incompatible profiles."""
    employee_id: uuid.UUID
    vector: list[float]


@dataclass(frozen=True)
class MeetingContext:
    starts_at_local: datetime        # aware datetime in the meeting's timezone
    timezone: str                    # IANA name, e.g. "Asia/Almaty"
    participants: list[Participant]
    asr_language: str | None = None  # auto/ru/kk; None preserves env for CLI callers
    asr_profile: str | None = None


def _never_cancelled() -> bool:
    return False


def _ignore_stage(_: Stage) -> None:
    return None


@dataclass(frozen=True)
class ProcessRequest:
    source_path: Path                # uploaded file, read only
    work_dir: Path                   # recording directory; AI writes normalized.wav here
    meeting: MeetingContext
    candidates: list[VoiceCandidate]
    on_stage: Callable[[Stage], None] = _ignore_stage
    is_cancelled: Callable[[], bool] = _never_cancelled
    run_extraction: bool = True      # False: stop after identification (extraction.status="not_started")


@dataclass
class SpeakerResult:
    label: str                            # SPEAKER_00...
    proposed_employee_id: uuid.UUID | None
    similarity: float | None              # cosine to best candidate; NOT a probability
    second_similarity: float | None       # None when there is no runner-up
    review_required: bool
    review_reasons: list[str]
    clean_speech_seconds: float
    speech_seconds: float = 0.0


@dataclass
class UtteranceResult:
    index: int                            # 0..n-1 in time order; backend maps to DB id
    start: float
    end: float
    speaker_label: str | None
    text: str
    language: str | None                  # ASR segment language (ru, kk, ...)
    uncertain_reasons: list[str]


@dataclass(frozen=True)
class TranscriptLine:
    """Extraction input: an utterance with its current speaker binding."""
    id: int                               # DB id (or UtteranceResult.index on first run)
    speaker_label: str | None
    employee_id: uuid.UUID | None
    text: str
    start: float


@dataclass
class TaskResult:
    task: str
    to_employee_id: uuid.UUID | None      # only from participants; None if not stated
    from_label: str | None                # speaker label of the first source line
    from_employee_id: uuid.UUID | None    # that line's employee binding, if known
    deadline: date | None                 # None if not stated
    deadline_source: str | None           # verbatim deadline phrase
    evidence: str                         # verbatim quote present in the source lines
    source_ids: list[int]                 # TranscriptLine.id
    confidence: float                     # [0, 1], model self-estimate, not a probability
    review_reasons: list[str]


@dataclass
class ExtractionOutcome:
    status: Literal["ok", "error", "not_started"]
    error_code: str | None = None         # EXTRACTION_ERROR_CODES
    error_message: str | None = None      # technical, no meeting content
    tasks: list[TaskResult] = field(default_factory=list)   # [] on error: NOT "no tasks"
    summary: str | None = None            # None on error


@dataclass
class StageTiming:
    stage: str
    seconds: float


@dataclass
class ProcessResult:
    normalized_path: Path                 # 16 kHz mono PCM WAV inside work_dir
    duration_seconds: float
    languages: list[str]
    speakers: list[SpeakerResult]
    utterances: list[UtteranceResult]
    extraction: ExtractionOutcome
    timings: list[StageTiming] = field(default_factory=list)


@dataclass(frozen=True)
class VoiceModelInfo:
    model_id: str
    revision: str
    dimension: int


@dataclass
class EnrollmentResult:
    vector: list[float]                   # L2-normalized
    model: VoiceModelInfo
    quality_status: Literal["ok", "needs_review"]
    reasons: list[str]                    # inconsistent_voice | clipping
    speech_seconds: float
    chunks: int = 0
    consistency: float | None = None      # min cosine chunk->mean


class AIError(Exception):
    """Processing failure with a code from AI_ERROR_CODES. ``message`` is user-facing
    and never contains meeting content."""

    def __init__(self, code: str, message: str, *, stage: Stage | None = None, model: str | None = None):
        super().__init__(code)
        self.code, self.message, self.stage, self.model = code, message, stage, model

    def __repr__(self) -> str:
        return f"AIError({self.code!r}, stage={self.stage!r}, model={self.model!r})"


class VoiceQualityRejected(Exception):
    """Enrollment sample rejected: too_short | silence | clipping."""

    def __init__(self, reasons: list[str], speech_seconds: float, required_seconds: float, message: str):
        super().__init__("VOICE_QUALITY_REJECTED")
        self.reasons, self.speech_seconds = reasons, speech_seconds
        self.required_seconds, self.message = required_seconds, message
