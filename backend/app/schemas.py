"""Request bodies and response models (see docs/API_CONTRACT.md)."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")

Role = Literal["admin", "secretary", "employee"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _tz(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown IANA timezone")
    return v


# ------------------------------------------------------------------ common


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class EmployeeBrief(BaseModel):
    id: uuid.UUID
    fio: str
    position: str
    department: str


class UserOut(BaseModel):
    id: uuid.UUID
    login: str
    role: Role
    employee: EmployeeBrief | None


class MeOut(UserOut):
    csrf_token: str


class LoginOut(BaseModel):
    user: UserOut
    csrf_token: str


# ------------------------------------------------------------------ auth/users


class LoginIn(Strict):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


LOGIN_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")


class UserCreate(Strict):
    employee_id: uuid.UUID
    login: str
    password: str = Field(min_length=8, max_length=256)
    role: Role

    @field_validator("login")
    @classmethod
    def _login(cls, v: str) -> str:
        if not LOGIN_RE.match(v):
            raise ValueError("login: 3-64 символа [A-Za-z0-9._-]")
        return v


class UserPatch(Strict):
    role: Role | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)
    active: bool | None = None


class UserAdminOut(BaseModel):
    id: uuid.UUID
    login: str
    role: Role
    active: bool
    employee_id: uuid.UUID | None
    employee_fio: str | None
    created_at: datetime


# ------------------------------------------------------------------ employees


class EmployeeCreate(Strict):
    fio: str = Field(min_length=1, max_length=255)
    position: str = Field(min_length=1, max_length=255)
    department: str = Field(min_length=1, max_length=255)


class EmployeePatch(Strict):
    fio: str | None = Field(default=None, min_length=1, max_length=255)
    position: str | None = Field(default=None, min_length=1, max_length=255)
    department: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None


class VoiceStatusOut(BaseModel):
    status: Literal["none", "ok", "needs_review", "incompatible"]
    quality_status: str | None = None
    speech_seconds: float | None = None
    created_at: datetime | None = None
    consent_at: datetime | None = None


class EmployeeOut(BaseModel):
    id: uuid.UUID
    fio: str
    position: str
    department: str
    active: bool
    has_account: bool
    user_id: uuid.UUID | None
    voice_profile: VoiceStatusOut


class VoiceEnrollmentOut(BaseModel):
    status: Literal["ok", "needs_review"]
    quality_status: Literal["ok", "needs_review"]
    speech_seconds: float
    reasons: list[str]
    created_at: datetime


# ------------------------------------------------------------------ meetings


class MeetingCreate(Strict):
    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime
    timezone: str
    agenda: str = Field(default="", max_length=20000)
    participant_ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)
    secretary_id: uuid.UUID | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v):
        return _tz(v)

    @field_validator("agenda", mode="before")
    @classmethod
    def _agenda(cls, v):
        return v or ""


class MeetingPatch(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    starts_at: datetime | None = None
    timezone: str | None = None
    agenda: str | None = Field(default=None, max_length=20000)
    participant_ids: list[uuid.UUID] | None = Field(default=None, max_length=200)
    secretary_id: uuid.UUID | None = None

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v):
        return None if v is None else _tz(v)


class UserRef(BaseModel):
    id: uuid.UUID
    login: str
    employee_id: uuid.UUID | None
    fio: str | None


class ParticipantOut(BaseModel):
    employee_id: uuid.UUID
    fio: str
    position: str
    department: str
    has_account: bool
    voice_status: str


class ExtractionOut(BaseModel):
    status: Literal["not_started", "ok", "error"]
    error_code: str | None
    error_message: str | None


class RecordingOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    processing_status: Literal["processing", "done", "error"]
    stage: str
    error_code: str | None
    error_message: str | None
    generation: int
    original_filename: str
    duration_seconds: float | None
    languages: list[str]
    extraction: ExtractionOut
    audio_url: str | None
    created_at: datetime
    updated_at: datetime


class SpeakerOut(BaseModel):
    label: str
    proposed_employee_id: uuid.UUID | None
    confirmed_employee_id: uuid.UUID | None
    manually_set: bool
    similarity: float | None
    second_similarity: float | None
    review_required: bool
    review_reasons: list[str]
    clean_speech_seconds: float
    utterance_count: int


class UtteranceOut(BaseModel):
    id: int
    speaker_label: str | None
    employee_id: uuid.UUID | None
    start: float
    end: float
    text: str
    language: str | None
    uncertain: bool
    uncertain_reasons: list[str]


class TaskOut(BaseModel):
    id: uuid.UUID
    meeting_id: uuid.UUID
    meeting_title: str
    from_: str | None = Field(alias="from", serialization_alias="from")
    from_speaker_label: str | None
    from_fio: str | None
    to: uuid.UUID | None
    to_fio: str | None
    task: str
    deadline: date | None
    deadline_source: str | None
    deadline_at: datetime | None
    evidence: str | None
    source_utterance_ids: list[int]
    confidence: float | None
    status: Literal["draft", "confirmed"]
    execution_status: Literal["in_progress", "completed"]
    completed_at: datetime | None
    overdue: bool
    needs_review: bool
    review_reasons: list[str]
    origin: Literal["llm", "manual"]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True)


class Permissions(BaseModel):
    can_edit: bool
    can_confirm: bool
    can_export_draft: bool


class MeetingListItemOut(BaseModel):
    id: uuid.UUID
    title: str
    starts_at: datetime
    timezone: str
    organizer: UserRef
    secretary: UserRef
    approval_status: Literal["draft", "confirmed"]
    protocol_version: int
    participant_count: int
    recording: RecordingOut | None
    can_edit: bool


class MeetingDetailOut(MeetingListItemOut):
    agenda: str
    draft_revision: int
    confirmed_at: datetime | None
    confirmed_by: uuid.UUID | None
    participants: list[ParticipantOut]
    permissions: Permissions
    content_visible: bool
    speakers: list[SpeakerOut]
    utterances: list[UtteranceOut]
    summary: str | None
    summary_edited: bool
    tasks: list[TaskOut]


# ------------------------------------------------------------------ review


class SummaryPatch(Strict):
    summary: str = Field(max_length=50000)


class SummaryOut(BaseModel):
    summary: str
    summary_edited: bool
    draft_revision: int


class SpeakersOut(BaseModel):
    speakers: list[SpeakerOut]
    draft_revision: int


class TaskCreate(Strict):
    task: str = Field(min_length=1, max_length=2000)
    to: uuid.UUID | None = None
    from_: str | None = Field(default=None, alias="from")
    deadline: date | None = None
    deadline_source: str | None = Field(default=None, max_length=500)
    evidence: str | None = Field(default=None, max_length=4000)
    source_utterance_ids: list[int] = Field(default_factory=list, max_length=50)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class TaskPatch(Strict):
    """Absent field = unchanged; explicit null clears (see model_fields_set)."""

    task: str | None = Field(default=None, min_length=1, max_length=2000)
    to: uuid.UUID | None = None
    from_: str | None = Field(default=None, alias="from")
    deadline: date | None = None
    deadline_source: str | None = Field(default=None, max_length=500)
    evidence: str | None = Field(default=None, max_length=4000)
    source_utterance_ids: list[int] | None = Field(default=None, max_length=50)
    needs_review: bool | None = None
    execution_status: Literal["in_progress", "completed"] | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ConfirmIn(Strict):
    draft_revision: int
    acknowledge_incomplete: bool = False


class NotificationOut(BaseModel):
    id: uuid.UUID
    event_type: str
    title: str
    message: str
    meeting_id: uuid.UUID | None
    task_id: uuid.UUID | None
    created_at: datetime
    read_at: datetime | None


class NotificationsPage(Page[NotificationOut]):
    unread_count: int


class NotificationPatch(Strict):
    read: bool


class ReadAllOut(BaseModel):
    updated: int


class HealthOut(BaseModel):
    status: str
    database: str


class SystemStatusOut(BaseModel):
    models: dict[str, Any]
    llm: dict[str, Any]
    tools: dict[str, bool]
    voice: dict[str, Any]
    jobs: dict[str, int]
