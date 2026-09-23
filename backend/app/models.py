"""ORM models. Schema changes go through Alembic migrations in ``migrations/``."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


# Enumerations are stored as short strings with CHECK-free validation in the API
# layer; the allowed values are documented in docs/API_CONTRACT.md.
ROLES = ("admin", "secretary", "employee")
PROCESSING_STATUSES = ("processing", "done", "error")
STAGES = (
    "queued",
    "normalizing",
    "transcribing",
    "diarizing",
    "aligning",
    "identifying",
    "extracting",
    "done",
)


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    fio: Mapped[str] = mapped_column(String(255))
    position: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user: Mapped[User | None] = relationship(back_populates="employee", uselist=False)
    voice_profile: Mapped[VoiceProfile | None] = relationship(
        back_populates="employee", uselist=False, cascade="all, delete-orphan"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), unique=True, nullable=True
    )
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    employee: Mapped[Employee | None] = relationship(back_populates="user")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    # sha256 of the session token; the raw token only lives in the cookie.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class VoiceProfile(Base):
    __tablename__ = "voice_profiles"

    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="CASCADE"), primary_key=True
    )
    vector: Mapped[list[float]] = mapped_column(Vector())
    model_id: Mapped[str] = mapped_column(String(255))
    model_revision: Mapped[str] = mapped_column(String(255))
    dimension: Mapped[int] = mapped_column(Integer)
    quality_status: Mapped[str] = mapped_column(String(32))
    quality_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    speech_seconds: Mapped[float] = mapped_column(Float)
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    employee: Mapped[Employee] = relationship(back_populates="voice_profile")


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(500))
    agenda: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64))
    organizer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    secretary_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    approval_status: Mapped[str] = mapped_column(String(16), default="draft")
    protocol_version: Mapped[int] = mapped_column(Integer, default=0)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    asr_language: Mapped[str] = mapped_column(String(8), default="auto", server_default="auto")
    asr_profile: Mapped[str] = mapped_column(String(16), default="refined", server_default="refined")
    meeting_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    organizer: Mapped[User] = relationship(foreign_keys=[organizer_id])
    secretary: Mapped[User] = relationship(foreign_keys=[secretary_id])
    participants: Mapped[list[MeetingParticipant]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )
    recording: Mapped[Recording | None] = relationship(
        back_populates="meeting", uselist=False, cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(back_populates="meeting", cascade="all, delete-orphan")


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"

    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="RESTRICT"), primary_key=True
    )

    meeting: Mapped[Meeting] = relationship(back_populates="participants")
    employee: Mapped[Employee] = relationship()


class Recording(Base):
    """The single active recording of a meeting (MVP: one per meeting)."""

    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), unique=True
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    local_path: Mapped[str] = mapped_column(String(1024))
    normalized_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(16), default="processing")
    stage: Mapped[str] = mapped_column(String(16), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    languages: Mapped[list] = mapped_column(JSONB, default=list)
    extraction_status: Mapped[str] = mapped_column(String(16), default="not_started")
    extraction_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extraction_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="recording")
    speakers: Mapped[list[Speaker]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Speaker.label"
    )
    utterances: Mapped[list[Utterance]] = relationship(
        back_populates="recording", cascade="all, delete-orphan", order_by="Utterance.start"
    )


class Speaker(Base):
    __tablename__ = "speakers"
    __table_args__ = (UniqueConstraint("recording_id", "label"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(String(32))
    proposed_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    manually_set: Mapped[bool] = mapped_column(Boolean, default=False)
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    second_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True)
    review_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    clean_speech_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    utterance_count: Mapped[int] = mapped_column(Integer, default=0)

    recording: Mapped[Recording] = relationship(back_populates="speakers")


class Utterance(Base):
    __tablename__ = "utterances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recording_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recordings.id", ondelete="CASCADE"), index=True
    )
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    speaker_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    uncertain_reasons: Mapped[list] = mapped_column(JSONB, default=list)

    recording: Mapped[Recording] = relationship(back_populates="utterances")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    # Author: explicit employee (manual edit) wins; otherwise the speaker label,
    # resolved through the speaker's current binding at read time.
    from_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    to_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True
    )
    task: Mapped[str] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_utterance_ids: Mapped[list] = mapped_column(JSONB, default=list)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    execution_status: Mapped[str] = mapped_column(String(16), default="in_progress")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list] = mapped_column(JSONB, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="llm")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="tasks")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default="")
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), nullable=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=True
    )
    dedup_key: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LiveSession(Base):
    """Browser recording session: ordered chunks of ONE MediaRecorder container."""

    __tablename__ = "live_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    meeting_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("meetings.id", ondelete="CASCADE"), index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(16), default="recording")  # recording|finalizing|cancelled|error
    local_path: Mapped[str] = mapped_column(String(1024))
    next_sequence: Mapped[int] = mapped_column(Integer, default=0)
    received_bytes: Mapped[int] = mapped_column(Integer, default=0)
    chunk_hashes: Mapped[list] = mapped_column(JSONB, default=list)  # sha256 per sequence
    preview_status: Mapped[str] = mapped_column(String(16), default="waiting")
    preview_error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    preview_utterances: Mapped[list] = mapped_column(JSONB, default=list)
    preview_bytes: Mapped[int] = mapped_column(Integer, default=0)
    preview_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # AI LivePreviewResult.to_dict()
    processed_until_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    recording_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    last_chunk_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
