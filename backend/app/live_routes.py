"""Live recording HTTP routes (docs/LIVE_CONTRACT.md) and meeting link editing."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from . import live_service
from .access import get_meeting_for_edit
from .auth import CurrentUser, get_current_user
from .config import get_settings
from .db import get_db
from .errors import ApiError, validation

router = APIRouter(prefix="/meetings", tags=["live"])


class LiveStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["microphone", "display"]
    mime_type: str = Field(min_length=1, max_length=128)


class LiveStartOut(BaseModel):
    session_id: str
    state: str
    next_sequence: int
    poll_after_ms: int
    max_chunk_bytes: int


class ChunkOut(BaseModel):
    accepted_sequence: int
    next_sequence: int


class LiveError(BaseModel):
    code: str
    message: str | None = None


class LiveUtterance(BaseModel):
    id: str
    start: float
    end: float
    speaker_label: str | None
    text: str
    is_final: bool


class LiveSnapshotOut(BaseModel):
    session_id: str
    state: Literal["recording", "finalizing", "done", "error", "cancelled"]
    next_sequence: int
    received_bytes: int
    processed_until_seconds: float
    received_audio_seconds: float | None
    lag_seconds: float | None
    revision: int
    preview_status: Literal["waiting", "processing", "ready", "unavailable"]
    preview_error: LiveError | None
    utterances: list[LiveUtterance]
    draft_tasks: list[dict]
    recording_id: str | None
    error: LiveError | None


class FinishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    last_sequence: int = Field(ge=-1)


class FinishOut(BaseModel):
    session_id: str
    state: str
    recording_id: str | None


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meeting_url: str | None = Field(default=None, max_length=2000)

    @field_validator("meeting_url")
    @classmethod
    def _url(cls, v):
        return validate_meeting_url(v)


class LinkOut(BaseModel):
    meeting_url: str | None


def validate_meeting_url(v: str | None) -> str | None:
    from urllib.parse import urlparse

    if v is None or not v.strip():
        return None
    v = v.strip()
    p = urlparse(v)
    if p.scheme.lower() not in ("http", "https") or not p.netloc or len(v) > 2000:
        raise ValueError("meeting_url: только http/https ссылка")
    return v


@router.post("/{meeting_id}/live", status_code=201, response_model=LiveStartOut)
def start_live(meeting_id: str, body: LiveStartIn, current: CurrentUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    s = live_service.start(db, meeting, current.id, body.source, body.mime_type)
    settings = get_settings()
    return LiveStartOut(session_id=str(s.id), state=s.state, next_sequence=0,
                        poll_after_ms=settings.live_poll_after_ms, max_chunk_bytes=settings.live_max_chunk_bytes)


@router.put("/{meeting_id}/live/{session_id}/chunks/{sequence}", response_model=ChunkOut,
            openapi_extra={"requestBody": {"required": True, "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                "audio/webm": {"schema": {"type": "string", "format": "binary"}}}}})
async def put_chunk(meeting_id: str, session_id: str, sequence: int, request: Request,
                    current: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    limit = get_settings().live_max_chunk_bytes
    if sequence < 0:
        raise validation("sequence >= 0", ["path", "sequence"])
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "FILE_TOO_LARGE", f"Чанк больше {limit} байт")
    body = bytearray()
    async for part in request.stream():
        body.extend(part)
        if len(body) > limit:
            raise ApiError(413, "FILE_TOO_LARGE", f"Чанк больше {limit} байт")
    if not body:
        raise validation("Пустой чанк", ["body"])

    def work():
        meeting = get_meeting_for_edit(db, meeting_id, current, require_draft=False)
        s = live_service.get_session(db, meeting, session_id, lock=True)
        return live_service.put_chunk(db, s, sequence, bytes(body)), s.id

    (out, wake), sid = await run_in_threadpool(work)
    if wake:
        live_service.kick(sid)  # one coalescing worker per session, not a task per chunk
    return out


@router.get("/{meeting_id}/live/{session_id}", response_model=LiveSnapshotOut)
def get_live(meeting_id: str, session_id: str, current: CurrentUser = Depends(get_current_user),
             db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, require_draft=False)
    return live_service.snapshot(db, live_service.get_session(db, meeting, session_id))


@router.post("/{meeting_id}/live/{session_id}/finish", status_code=202, response_model=FinishOut)
def finish_live(meeting_id: str, session_id: str, body: FinishIn, background: BackgroundTasks,
                current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True, require_draft=False)
    s = live_service.get_session(db, meeting, session_id, lock=True)
    return live_service.finish(db, meeting, s, body.last_sequence, background)


@router.post("/{meeting_id}/live/{session_id}/cancel", status_code=204, response_class=Response)
def cancel_live(meeting_id: str, session_id: str, current: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, require_draft=False)
    live_service.cancel(db, live_service.get_session(db, meeting, session_id, lock=True))
    return Response(status_code=204)


@router.patch("/{meeting_id}/link", response_model=LinkOut)
def patch_link(meeting_id: str, body: LinkIn, current: CurrentUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """Stored as text only: the server never opens or fetches the URL."""
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True, require_draft=False)
    meeting.meeting_url = body.meeting_url
    db.commit()
    return LinkOut(meeting_url=meeting.meeting_url)


class SpeechSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asr_language: Literal["auto", "ru", "kk"]
    asr_profile: Literal["standard", "refined"]


@router.patch("/{meeting_id}/speech-settings", response_model=SpeechSettings)
def update_speech_settings(meeting_id: str, body: SpeechSettings,
                           current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    from sqlalchemy import select
    from .models import LiveSession
    from .errors import conflict

    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    if (meeting.recording and meeting.recording.processing_status == "processing") or db.scalar(
        select(LiveSession.id).where(LiveSession.meeting_id == meeting.id, LiveSession.state == "recording")
    ):
        raise conflict("RECORDING_PROCESSING", "Дождитесь завершения записи и обработки")
    meeting.asr_language = body.asr_language
    meeting.asr_profile = body.asr_profile
    db.commit()
    return body
