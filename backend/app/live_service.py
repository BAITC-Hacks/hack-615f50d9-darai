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
import time
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
    decoded = (s.preview_state or {}).get("decoded_seconds")
    received_audio = round(float(decoded), 2) if isinstance(decoded, (int, float)) and decoded > 0 else None
    return {
        "session_id": str(s.id), "state": state, "next_sequence": s.next_sequence,
        "received_bytes": s.received_bytes, "processed_until_seconds": round(s.processed_until_seconds, 2),
        "received_audio_seconds": received_audio,
        "lag_seconds": None if received_audio is None else round(max(0.0, received_audio - s.processed_until_seconds), 2),
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
    # The worker itself coalesces by time; the chunk only wakes it (docs/LIVE_CONTRACT.md §3).
    return {"accepted_sequence": sequence, "next_sequence": s.next_sequence}, settings.live_preview_enabled


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
    stop_preview(s.id)  # late preview results must not land in a finalizing session
    src = Path(s.local_path)
    with open(src, "rb") as fh:
        upload = SimpleNamespace(filename=f"live-{s.source}{src.suffix}", file=fh)
        rec = _store_recording(db, meeting, upload, background)  # commits (incl. state) and queues the job
    s.recording_id = rec.id
    db.commit()
    shutil.rmtree(src.parent, ignore_errors=True)  # the container now lives in the recording directory
    return {"session_id": str(s.id), "state": "finalizing", "recording_id": str(rec.id)}


def cancel(db: Session, s: LiveSession) -> None:
    stop_preview(s.id)
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
    for s in rows:
        stop_preview(s.id)
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
    for s in rows:
        stop_preview(s.id)
    return len(rows)


# ------------------------------------------------------------------ preview worker
#
# One daemon thread per recording session (started by the first chunk, woken by
# the following ones) instead of a BackgroundTask per chunk: updates coalesce,
# a session never has two preview calls, and delayed retries sleep on an Event
# instead of piling up tasks. Three counters are kept apart: received_bytes
# (stored), decoded_seconds (measured by the AI on the snapshot) and
# processed_until_seconds (recognized). See docs/LIVE_CONTRACT.md §3.

_preview_sem = threading.BoundedSemaphore(1)  # one preview call at a time across sessions
# Last AI result per session (carries the AI's incremental state); also persisted as
# preview_state (result.to_dict()) so a restart does not force a from-scratch preview.
_last_results: dict[uuid.UUID, Any] = {}
_workers: dict[uuid.UUID, "_Worker"] = {}
_workers_lock = threading.Lock()


def kick(session_id: uuid.UUID) -> None:
    """New bytes stored: wake (or start) the session's preview worker. Cheap, never blocks."""
    if not get_settings().live_preview_enabled:
        return
    with _workers_lock:
        w = _workers.get(session_id)
        if w is None:
            w = _workers[session_id] = _Worker(session_id)
            w.thread.start()
    w.wake.set()


def stop_preview(session_id: uuid.UUID) -> None:
    """cancel/finish/state change: stop the worker; its late results are not published."""
    with _workers_lock:
        w = _workers.pop(session_id, None)
    if w is not None:
        w.stopped.set()
        w.wake.set()
    _last_results.pop(session_id, None)


def _set_preview(session_id, **values) -> None:
    with db_session() as db:
        s = db.scalar(select(LiveSession).where(LiveSession.id == session_id).with_for_update())
        if s is None or s.state != "recording":
            return
        for k, v in values.items():
            setattr(s, k, v)
        db.commit()


def _utterances(result) -> list[dict]:
    return [{"id": u.id, "start": float(u.start), "end": float(u.end), "speaker_label": u.speaker_label,
             "text": u.text, "is_final": bool(u.is_final)} for u in result.utterances]


class _Worker:
    def __init__(self, session_id: uuid.UUID):
        self.sid = session_id
        self.wake = threading.Event()     # a new chunk was stored
        self.stopped = threading.Event()  # cancel/finish: stop and never publish again
        self.thread = threading.Thread(target=self._main, name=f"live-preview-{session_id.hex[:8]}", daemon=True)
        self.snap: Path | None = None     # snapshot kept while the AI reports pending audio / retry
        self.snap_bytes = 0

    # ---------------------------------------------------------------- helpers

    def _current(self) -> bool:
        with _workers_lock:
            return not self.stopped.is_set() and _workers.get(self.sid) is self

    def _drop_snap(self) -> None:
        if self.snap is not None:
            self.snap.unlink(missing_ok=True)
        self.snap, self.snap_bytes = None, 0

    def _publish(self, result, stable: int, *, status: str, done: bool) -> bool:
        """Persist a FULL AI snapshot if the session is still ours; revision grows only on new content."""
        utts = _utterances(result)
        processed = float(result.processed_until_seconds)
        decoded = getattr(result, "decoded_seconds", None)
        state_dict = result.to_dict() if hasattr(result, "to_dict") else (
            {"decoded_seconds": float(decoded)} if decoded is not None else None)
        with db_session() as db:
            s = db.scalar(select(LiveSession).where(LiveSession.id == self.sid).with_for_update())
            if s is None or s.state != "recording" or not self._current():
                return False
            if utts != list(s.preview_utterances or []) or abs(processed - s.processed_until_seconds) > 1e-6:
                s.preview_utterances = utts
                s.processed_until_seconds = processed
                s.revision += 1
            if state_dict is not None:
                s.preview_state = state_dict
            if done:
                s.preview_bytes = stable
            s.preview_status, s.preview_error = status, getattr(result, "preview_error", None)
            db.commit()
        _last_results[self.sid] = result
        return True

    def _load(self) -> dict | None:
        with db_session() as db:
            s = db.get(LiveSession, self.sid)
            if s is None or s.state != "recording":
                return None
            meeting = db.get(Meeting, s.meeting_id)
            return {"received": s.received_bytes, "src": Path(s.local_path), "mime": s.mime_type,
                    "prev_utts": s.preview_utterances, "prev_until": s.processed_until_seconds,
                    "prev_state": s.preview_state, "status": s.preview_status,
                    "asr_language": meeting.asr_language, "asr_profile": meeting.asr_profile}

    # ---------------------------------------------------------------- loop

    def _main(self) -> None:
        try:
            self._loop()
        except Exception as exc:  # never kill the recording because of preview
            log.error("live preview worker failed: session=%s err=%s", self.sid, type(exc).__name__)
        finally:
            with _workers_lock:
                if _workers.get(self.sid) is self:
                    del _workers[self.sid]
            self._drop_snap()

    def _loop(self) -> None:
        from .processing import jobs

        settings = get_settings()
        interval = settings.live_preview_check_interval_ms / 1000
        attempted = 0          # bytes of the last snapshot fully handled (processed or "not enough speech")
        force = False          # run again without new bytes: pending backlog or model-busy retry
        not_before = 0.0       # monotonic: coalescing interval / retry delay / error backoff
        while not self.stopped.is_set():
            delay = not_before - time.monotonic()
            if delay > 0:
                self.stopped.wait(delay)  # new chunks do not shorten the delay: that is the coalescing
                continue
            self.wake.clear()
            info = self._load()
            if info is None:
                return  # finalizing / cancelled / error: nothing to preview any more
            if not force and info["received"] - attempted < settings.live_preview_min_new_bytes:
                self.wake.wait(interval)  # not enough data: sleep until the next chunk (or re-check state)
                continue
            if jobs.running or jobs.queued:  # full processing has priority
                if info["status"] != "waiting":
                    _set_preview(self.sid, preview_status="waiting")
                not_before = time.monotonic() + interval
                continue
            if not _preview_sem.acquire(timeout=interval):
                continue  # another session is previewing
            try:
                started = time.monotonic()
                outcome = self._attempt(info, jobs)
            finally:
                _preview_sem.release()
            kind, value = outcome
            if kind == "stop":
                return
            if kind == "pending":      # backlog in the same snapshot: continue right away
                force, not_before = True, 0.0
            elif kind == "retry":      # model busy: one delayed retry, no busy loop
                force, not_before = True, time.monotonic() + value
            elif kind == "error":
                force, attempted = False, value
                not_before = time.monotonic() + settings.live_preview_error_backoff_ms / 1000
            else:                      # "idle": snapshot handled; wait for new bytes
                force, attempted = False, value
                not_before = started + interval

    def _attempt(self, info: dict, jobs) -> tuple[str, Any]:
        settings = get_settings()
        src = info["src"]
        try:
            mod, T = _live_ai()
        except ai_gateway.AIUnavailable as exc:
            _set_preview(self.sid, preview_status="unavailable",
                         preview_error={"code": "MODEL_UNAVAILABLE", "message": exc.message})
            return "error", info["received"]
        if self.snap is None:
            stable = info["received"]
            snap = src.parent / f"snap-{stable}{src.suffix}"
            try:
                with open(src, "rb") as fi, open(snap, "wb") as fo:
                    fo.write(fi.read(stable))
            except OSError:
                return "stop", None  # cancelled and removed meanwhile
            self.snap, self.snap_bytes = snap, stable
        stable, snap = self.snap_bytes, self.snap
        work = src.parent / "preview"
        work.mkdir(exist_ok=True)

        previous = _last_results.get(self.sid)
        prev_state = info["prev_state"]
        if previous is None and prev_state and "state" in prev_state and hasattr(T.LivePreviewResult, "from_dict"):
            try:
                previous = T.LivePreviewResult.from_dict(prev_state)
            except Exception:
                previous = None  # losing previous only means a from-scratch preview
        if previous is None and info["prev_utts"]:
            previous = T.LivePreviewResult(processed_until_seconds=info["prev_until"],
                                           utterances=[T.LivePreviewUtterance(**u) for u in info["prev_utts"]])
        before = float(previous.processed_until_seconds) if previous is not None else 0.0
        if info["status"] != "processing":
            _set_preview(self.sid, preview_status="processing")

        def cancelled() -> bool:
            if self.stopped.is_set() or jobs.running:
                return True
            with db_session() as db:
                cur = db.get(LiveSession, self.sid)
                return cur is None or cur.state != "recording"

        def on_update(partial) -> None:  # after every ASR window: show text now, not after the whole call
            if not self._publish(partial, stable, status="processing", done=False):
                self.stopped.set()  # session gone: make the AI stop at its next check

        kwargs = dict(container_path=snap, mime_type=info["mime"], stable_bytes=stable, work_dir=work,
                      previous=previous, is_cancelled=cancelled,
                      asr_language=info["asr_language"], asr_profile=info["asr_profile"])
        if "on_update" in getattr(T.LivePreviewRequest, "__dataclass_fields__", {}):
            kwargs["on_update"] = on_update
        try:
            result = mod.transcribe_preview(T.LivePreviewRequest(**kwargs))
        except Exception as exc:
            code = getattr(exc, "code", None) or "PREVIEW_FAILED"
            if code == "CANCELLED":
                if self.stopped.is_set():
                    return "stop", None
                _set_preview(self.sid, preview_status="waiting")  # final processing started
                return "retry", settings.live_preview_check_interval_ms / 1000
            log.warning("live preview failed: session=%s code=%s", self.sid, code)
            self._drop_snap()
            _set_preview(self.sid, preview_status="unavailable",
                         preview_error={"code": code, "message": getattr(exc, "message", code)})
            return "error", stable

        status = getattr(result, "preview_status", "ready") or "ready"
        retry_ms = getattr(result, "retry_after_ms", None)
        progressed = float(result.processed_until_seconds) > before + 1e-6
        # has_pending_audio without progress would loop forever on the same snapshot: treat as idle.
        pending = bool(getattr(result, "has_pending_audio", False)) and progressed and status == "ready"
        if retry_ms is not None:
            kind, value = "retry", min(max(int(retry_ms), 100), settings.live_preview_retry_max_ms) / 1000
            shown = "waiting"
        elif pending:
            kind, value, shown = "pending", None, "processing"
        else:
            kind, value, shown = ("error" if status == "unavailable" else "idle"), stable, status
        if not self._publish(result, stable, status=shown, done=kind == "idle" and status == "ready"):
            return "stop", None
        if kind in ("idle", "error"):
            self._drop_snap()  # a retry reuses the same snapshot as well as pending backlog
        return kind, value
