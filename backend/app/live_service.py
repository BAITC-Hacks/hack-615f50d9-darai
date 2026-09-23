"""Live browser recording: ordered chunk storage, preview scheduling, finalization.

MediaRecorder(timeslice) chunks are parts of ONE container: bytes are appended
strictly by sequence to a single file and never decoded chunk by chunk. Preview
runs on a copy of the stable prefix (the AI layer tolerates a truncated tail).
On finish the whole container is handed to the existing recording pipeline.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import ai_gateway
from .config import get_settings
from .db import db_session
from .errors import ApiError, conflict, validation
from .models import LiveSession, Meeting, Recording, utcnow

log = logging.getLogger("darai.live")

MIME_RE = re.compile(r"^(audio|video)/(webm|ogg|mp4)(\s*;.*)?$", re.I)
_EXT = {"webm": ".webm", "ogg": ".ogg", "mp4": ".mp4"}
RESUMABLE_ERRORS = {"CLIENT_TIMEOUT", "INTERRUPTED"}


def container_ext(mime: str) -> str:
    m = MIME_RE.match(mime.strip())
    return _EXT[m.group(2).lower()] if m else ".bin"


def live_dir(meeting_id, session_id) -> Path:
    return get_settings().data_dir / "live" / str(meeting_id) / str(session_id)


# ------------------------------------------------------------------ AI adapter

_test_adapter: Any | None = None


def set_live_test_adapter(adapter: Any | None) -> None:
    """Tests only."""
    global _test_adapter
    _test_adapter = adapter


def _live_ai() -> tuple[Any, Any]:
    if _test_adapter is not None:
        return _test_adapter, _test_adapter.types
    try:
        return importlib.import_module("app.live_ai"), importlib.import_module("app.live_ai_types")
    except Exception as exc:
        raise ai_gateway.AIUnavailable(f"Live-preview недоступен: {type(exc).__name__}")


# ------------------------------------------------------------------ snapshot


def snapshot(db: Session, s: LiveSession) -> dict:
    state, error = s.state, s.error
    if s.state == "finalizing":
        rec = db.get(Recording, s.recording_id) if s.recording_id else None
        if rec is None or rec.processing_status == "done":
            state, error = "done", None
        elif rec.processing_status == "error":
            state, error = "error", {"code": rec.error_code or "ERROR", "message": rec.error_message or ""}
    return {
        "session_id": str(s.id), "state": state, "next_sequence": s.next_sequence,
        "received_bytes": s.received_bytes, "processed_until_seconds": round(s.processed_until_seconds, 2),
        "revision": s.revision, "preview_status": s.preview_status, "preview_error": s.preview_error,
        "utterances": list(s.preview_utterances or []), "draft_tasks": [],
        "recording_id": str(s.recording_id) if s.recording_id else None, "error": error,
    }


def get_session(db: Session, meeting: Meeting, session_id: str, *, lock: bool = False) -> LiveSession:
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise ApiError(404, "NOT_FOUND", "Сессия записи не найдена")
    stmt = select(LiveSession).where(LiveSession.id == sid, LiveSession.meeting_id == meeting.id)
    if lock:
        stmt = stmt.with_for_update()
    s = db.scalar(stmt)
    if s is None:
        raise ApiError(404, "NOT_FOUND", "Сессия записи не найдена")
    return s


# ------------------------------------------------------------------ start / chunks


def start(db: Session, meeting: Meeting, user_id, source: str, mime_type: str) -> LiveSession:
    if not MIME_RE.match(mime_type.strip()):
        raise validation("Неподдерживаемый mime_type", ["body", "mime_type"])
    if meeting.recording is not None and meeting.recording.processing_status == "processing":
        raise conflict("RECORDING_PROCESSING", "Запись встречи ещё обрабатывается")
    active = db.scalar(select(LiveSession.id).where(LiveSession.meeting_id == meeting.id,
                                                   LiveSession.state == "recording"))
    if active is not None:
        raise conflict("LIVE_ALREADY_ACTIVE", "У встречи уже идёт запись", {"session_id": str(active)})
    sid = uuid.uuid4()
    path = live_dir(meeting.id, sid) / f"stream{container_ext(mime_type)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    s = LiveSession(id=sid, meeting_id=meeting.id, created_by=user_id, source=source, mime_type=mime_type.strip(),
                    state="recording", local_path=str(path), next_sequence=0, received_bytes=0, chunk_hashes=[],
                    preview_status="waiting", preview_utterances=[], preview_bytes=0, processed_until_seconds=0.0,
                    revision=0, last_chunk_at=utcnow())
    db.add(s)
    try:
        db.commit()
    except IntegrityError:  # partial unique index: concurrent start
        db.rollback()
        shutil.rmtree(path.parent, ignore_errors=True)
        raise conflict("LIVE_ALREADY_ACTIVE", "У встречи уже идёт запись")
    return s


def put_chunk(db: Session, s: LiveSession, sequence: int, body: bytes) -> tuple[dict, bool]:
    """Returns (response, should_schedule_preview). `s` must be locked FOR UPDATE."""
    settings = get_settings()
    if s.state != "recording":
        raise conflict("LIVE_NOT_RECORDING", "Сессия не принимает данные", {"state": s.state})
    digest = hashlib.sha256(body).hexdigest()
    if sequence < s.next_sequence:
        if s.chunk_hashes[sequence] == digest:
            return {"accepted_sequence": sequence, "next_sequence": s.next_sequence}, False
        raise conflict("CHUNK_CONFLICT", "Для этого номера уже принято другое содержимое",
                       {"sequence": sequence, "next_sequence": s.next_sequence})
    if sequence > s.next_sequence:
        raise conflict("CHUNK_OUT_OF_ORDER", "Пропущен чанк", {"next_sequence": s.next_sequence})
    if s.received_bytes + len(body) > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(413, "FILE_TOO_LARGE", f"Запись больше {settings.max_upload_mb} МБ")
    path = Path(s.local_path)
    # Truncate to the committed size first: a crash between write and commit
    # must not leave stray bytes in the container.
    with open(path, "r+b") as f:
        f.truncate(s.received_bytes)
        f.seek(s.received_bytes)
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    s.chunk_hashes = list(s.chunk_hashes) + [digest]
    s.received_bytes += len(body)
    s.next_sequence += 1
    s.last_chunk_at = utcnow()
    s.error = None
    db.commit()
    schedule = settings.live_preview_enabled and \
        s.received_bytes - s.preview_bytes >= settings.live_preview_min_new_bytes
    return {"accepted_sequence": sequence, "next_sequence": s.next_sequence}, schedule


# ------------------------------------------------------------------ finish / cancel


def finish(db: Session, meeting: Meeting, s: LiveSession, last_sequence: int, background: BackgroundTasks) -> dict:
    """`meeting` and `s` locked by the caller."""
    from .routers.meetings import _store_recording  # reuse upload rules (replacement, ffprobe, limits)

    if s.state == "finalizing":
        return {"session_id": str(s.id), "state": "finalizing", "recording_id": str(s.recording_id)}
    if s.state == "cancelled" or (s.state == "error" and (s.error or {}).get("code") not in RESUMABLE_ERRORS):
        raise conflict("LIVE_NOT_RECORDING", "Сессию нельзя завершить", {"state": s.state})
    if last_sequence >= s.next_sequence:
        raise conflict("CHUNKS_MISSING", "Получены не все чанки", {"next_sequence": s.next_sequence})
    if last_sequence != s.next_sequence - 1:
        raise validation("last_sequence не совпадает с принятыми чанками", ["body", "last_sequence"])
    if s.received_bytes == 0:
        raise ApiError(422, "LIVE_EMPTY", "Запись пуста")
    if meeting.approval_status != "draft":
        raise conflict("MEETING_CONFIRMED", "Протокол уже подтверждён")
    s.state = "finalizing"
    src = Path(s.local_path)
    with open(src, "rb") as fh:
        upload = SimpleNamespace(filename=f"live-{s.source}{src.suffix}", file=fh)
        rec = _store_recording(db, meeting, upload, background)  # commits (incl. state) and queues the job
    s.recording_id = rec.id
    db.commit()
    _last_results.pop(s.id, None)
    shutil.rmtree(src.parent, ignore_errors=True)  # the container now lives in the recording directory
    return {"session_id": str(s.id), "state": "finalizing", "recording_id": str(rec.id)}


def cancel(db: Session, s: LiveSession) -> None:
    _last_results.pop(s.id, None)
    if s.state in ("recording", "error"):
        s.state = "cancelled"
        db.commit()
        shutil.rmtree(Path(s.local_path).parent, ignore_errors=True)


# ------------------------------------------------------------------ lifecycle


def recover_interrupted(db: Session) -> int:
    rows = db.scalars(select(LiveSession).where(LiveSession.state == "recording").with_for_update()).all()
    for s in rows:
        s.state = "error"
        s.error = {"code": "INTERRUPTED",
                   "message": "Сервер перезапускался. Принятые данные сохранены: завершите или отмените запись."}
    db.commit()
    return len(rows)


def expire_idle(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    limit = now - timedelta(seconds=get_settings().live_idle_timeout_seconds)
    rows = db.scalars(select(LiveSession).where(LiveSession.state == "recording", LiveSession.last_chunk_at < limit)
                      .with_for_update(skip_locked=True)).all()
    for s in rows:
        s.state = "error"
        s.error = {"code": "CLIENT_TIMEOUT",
                   "message": "Клиент не присылал данные. Принятые данные сохранены: завершите или отмените запись."}
    db.commit()
    return len(rows)


# ------------------------------------------------------------------ preview worker

_running: set[uuid.UUID] = set()
_running_lock = threading.Lock()
_preview_sem = threading.BoundedSemaphore(1)  # one preview at a time across sessions
# Last full AI result per session (carries the AI's incremental state); also persisted as
# preview_state (result.to_dict()) so a restart does not force a from-scratch preview.
_last_results: dict[uuid.UUID, Any] = {}


def _set_preview(session_id, **values) -> None:
    with db_session() as db:
        s = db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
        if s is None:
            return
        for k, v in values.items():
            setattr(s, k, v)
        db.commit()


def run_preview(session_id: uuid.UUID) -> None:
    """Coalescing preview loop for one session (BackgroundTasks, worker thread)."""
    from .processing import jobs

    with _running_lock:
        if session_id in _running:
            return  # an active loop will pick up the new bytes
        _running.add(session_id)
    try:
        if not _preview_sem.acquire(blocking=False):
            return  # another session is previewing; next chunk re-triggers
        try:
            _preview_loop(session_id, jobs)
        finally:
            _preview_sem.release()
    finally:
        with _running_lock:
            _running.discard(session_id)


def _preview_loop(session_id: uuid.UUID, jobs) -> None:
    settings = get_settings()
    while True:
        if jobs.running or jobs.queued:  # final processing has priority
            _set_preview(session_id, preview_status="waiting")
            return
        with db_session() as db:
            s = db.get(LiveSession, session_id)
            if s is None or s.state != "recording":
                _last_results.pop(session_id, None)
                return
            stable = s.received_bytes
            if stable - s.preview_bytes < settings.live_preview_min_new_bytes:
                return
            src, mime, prev_utts, prev_until = Path(s.local_path), s.mime_type, s.preview_utterances, \
                s.processed_until_seconds
            prev_state = s.preview_state
            meeting = db.get(Meeting, s.meeting_id)
            asr_language, asr_profile = meeting.asr_language, meeting.asr_profile
        try:
            mod, T = _live_ai()
        except ai_gateway.AIUnavailable as exc:
            _set_preview(session_id, preview_status="unavailable",
                         preview_error={"code": "MODEL_UNAVAILABLE", "message": exc.message}, preview_bytes=stable)
            return
        work = src.parent / "preview"
        work.mkdir(exist_ok=True)
        snap = src.parent / f"snap-{stable}{src.suffix}"
        try:
            with open(src, "rb") as fi, open(snap, "wb") as fo:
                fo.write(fi.read(stable))
        except OSError:
            return  # cancelled and removed meanwhile
        previous = _last_results.get(session_id)
        if previous is None and prev_state and hasattr(T.LivePreviewResult, "from_dict"):
            try:
                previous = T.LivePreviewResult.from_dict(prev_state)
            except Exception:
                previous = None  # losing previous only means a from-scratch preview
        if previous is None and prev_utts:
            previous = T.LivePreviewResult(processed_until_seconds=prev_until,
                                           utterances=[T.LivePreviewUtterance(**u) for u in prev_utts])
        _set_preview(session_id, preview_status="processing")

        def cancelled() -> bool:
            with db_session() as db:
                cur = db.get(LiveSession, session_id)
                return cur is None or cur.state != "recording" or bool(jobs.running)

        req = T.LivePreviewRequest(container_path=snap, mime_type=mime, stable_bytes=stable, work_dir=work,
                                   previous=previous, is_cancelled=cancelled,
                                   asr_language=asr_language, asr_profile=asr_profile)
        try:
            result = mod.transcribe_preview(req)
        except Exception as exc:
            snap.unlink(missing_ok=True)
            code = getattr(exc, "code", None) or "PREVIEW_FAILED"
            if code == "CANCELLED":
                _set_preview(session_id, preview_status="waiting")
                return
            log.warning("live preview failed: session=%s code=%s", session_id, code)
            _set_preview(session_id, preview_status="unavailable", preview_bytes=stable,
                         preview_error={"code": code, "message": getattr(exc, "message", code)})
            return
        snap.unlink(missing_ok=True)
        _last_results[session_id] = result
        state_dict = result.to_dict() if hasattr(result, "to_dict") else None
        status = getattr(result, "preview_status", "ready") or "ready"
        perr = getattr(result, "preview_error", None)
        utts = [{"id": u.id, "start": float(u.start), "end": float(u.end), "speaker_label": u.speaker_label,
                 "text": u.text, "is_final": bool(u.is_final)} for u in result.utterances]
        with db_session() as db:
            s = db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
            if s is None or s.state not in ("recording", "finalizing"):
                return
            s.preview_utterances = utts
            s.preview_state = state_dict
            s.processed_until_seconds = float(result.processed_until_seconds)
            s.preview_bytes = stable
            s.revision += 1
            s.preview_status, s.preview_error = status, perr
            db.commit()
