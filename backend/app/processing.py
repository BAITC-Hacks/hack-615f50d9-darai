"""Background recording jobs: concurrency limit, stage/error persistence,
generation fencing and restart recovery.

The heavy work is done by the AI pipeline (``ai_gateway``); this module only
prepares inputs from the DB and stores results atomically.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import ai_gateway
from .config import get_settings
from .db import db_session
from .models import Meeting, Recording, Speaker, Task, Utterance, VoiceProfile, utcnow
from .notifications import notify_processing_result
from .serializers import profile_compatible

log = logging.getLogger("darai.jobs")


class _JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.queued = 0
        self._sem: threading.BoundedSemaphore | None = None

    def semaphore(self) -> threading.BoundedSemaphore:
        with self.lock:
            if self._sem is None:
                self._sem = threading.BoundedSemaphore(get_settings().max_concurrent_jobs)
            return self._sem


jobs = _JobState()


def jobs_status() -> dict[str, int]:
    return {"max_concurrent": get_settings().max_concurrent_jobs, "running": jobs.running, "queued": jobs.queued}


def recording_dir(meeting_id: uuid.UUID, recording_id: uuid.UUID) -> Path:
    return get_settings().recordings_dir / str(meeting_id) / str(recording_id)


# ------------------------------------------------------------------ state updates


def _set_stage(recording_id: uuid.UUID, generation: int, stage: str) -> bool:
    with db_session() as db:
        res = db.execute(
            update(Recording)
            .where(Recording.id == recording_id, Recording.generation == generation,
                   Recording.processing_status == "processing")
            .values(stage=stage, updated_at=utcnow())
        )
        db.commit()
        return res.rowcount == 1


def _is_current(recording_id: uuid.UUID, generation: int) -> bool:
    with db_session() as db:
        row = db.execute(select(Recording.generation, Recording.processing_status)
                         .where(Recording.id == recording_id)).first()
        return row is not None and row[0] == generation and row[1] == "processing"


def _lock_current(db: Session, recording_id: uuid.UUID, generation: int) -> Recording | None:
    rec = db.scalar(select(Recording).where(Recording.id == recording_id).with_for_update())
    if rec is None or rec.generation != generation or rec.processing_status != "processing":
        return None
    return rec


def _mark_error(recording_id: uuid.UUID, generation: int, code: str, message: str,
                stage: str | None = None) -> None:
    with db_session() as db:
        rec = _lock_current(db, recording_id, generation)
        if rec is None:
            db.rollback()
            return
        rec.processing_status = "error"
        if stage:
            rec.stage = stage
        rec.error_code = code
        rec.error_message = message[:1000]
        meeting = db.get(Meeting, rec.meeting_id)
        assert meeting is not None
        notify_processing_result(db, meeting, rec)
        db.commit()
    log.warning("recording job failed: recording=%s generation=%s code=%s", recording_id, generation, code)


# ------------------------------------------------------------------ inputs


def _meeting_context(db: Session, meeting: Meeting):
    T = ai_gateway.types()
    local = meeting.starts_at.astimezone(ZoneInfo(meeting.timezone))
    participants = [T.Participant(employee_id=p.employee.id, fio=p.employee.fio, position=p.employee.position)
                    for p in sorted(meeting.participants, key=lambda p: p.employee.fio)]
    return T.MeetingContext(starts_at_local=local, timezone=meeting.timezone, participants=participants)


def _candidates(db: Session, meeting: Meeting):
    """Only invited participants with a profile compatible with the current voice model."""
    T = ai_gateway.types()
    info = ai_gateway.voice_model_info()
    ids = [p.employee_id for p in meeting.participants]
    if not ids:
        return []
    profiles = db.scalars(select(VoiceProfile).where(VoiceProfile.employee_id.in_(ids))).all()
    return [T.VoiceCandidate(employee_id=p.employee_id, vector=[float(x) for x in p.vector])
            for p in profiles if profile_compatible(p, info)]


# ------------------------------------------------------------------ persistence


def _insert_tasks(db: Session, meeting: Meeting, results, id_map: dict[int, int]) -> None:
    participants = {p.employee_id for p in meeting.participants}
    for t in results:
        sources = [id_map[i] for i in t.source_ids if i in id_map]
        reasons = list(t.review_reasons)
        to_employee = t.to_employee_id if t.to_employee_id in participants else None
        if to_employee is None and "missing_assignee" not in reasons:
            reasons.append("missing_assignee")
        # Keep the speaker label so the author follows later speaker re-binding;
        # a fixed employee is stored only when there is no label.
        from_employee = None if t.from_label else getattr(t, "from_employee_id", None)
        db.add(Task(
            meeting_id=meeting.id, from_label=t.from_label, from_employee_id=from_employee,
            to_employee_id=to_employee,
            task=t.task, deadline=t.deadline, deadline_source=t.deadline_source, evidence=t.evidence,
            source_utterance_ids=sources, confidence=t.confidence, status="draft",
            execution_status="in_progress", needs_review=bool(reasons), review_reasons=reasons, origin="llm",
        ))


def _apply_extraction(rec: Recording, meeting: Meeting, outcome) -> None:
    rec.extraction_status = outcome.status
    rec.extraction_error_code = outcome.error_code if outcome.status == "error" else None
    rec.extraction_error_message = outcome.error_message if outcome.status == "error" else None
    if outcome.status == "ok" and not meeting.summary_edited:
        meeting.summary = outcome.summary


def _store_full_result(recording_id: uuid.UUID, generation: int, result) -> bool:
    with db_session() as db:
        rec = _lock_current(db, recording_id, generation)
        if rec is None:
            db.rollback()
            return False
        meeting = db.scalar(select(Meeting).where(Meeting.id == rec.meeting_id).with_for_update())
        assert meeting is not None
        participant_ids = {p.employee_id for p in meeting.participants}
        db.execute(delete(Speaker).where(Speaker.recording_id == rec.id))
        db.execute(delete(Utterance).where(Utterance.recording_id == rec.id))
        db.execute(delete(Task).where(Task.meeting_id == meeting.id, Task.status == "draft"))

        id_map: dict[int, int] = {}
        counts: dict[str, int] = {}
        for u in sorted(result.utterances, key=lambda u: (u.start, u.index)):
            row = Utterance(recording_id=rec.id, start=float(u.start), end=float(u.end),
                            speaker_label=u.speaker_label, text=u.text, language=u.language,
                            uncertain=bool(u.uncertain_reasons), uncertain_reasons=list(u.uncertain_reasons))
            db.add(row)
            db.flush()
            id_map[u.index] = row.id
            if u.speaker_label:
                counts[u.speaker_label] = counts.get(u.speaker_label, 0) + 1
        for s in result.speakers:
            proposed = s.proposed_employee_id if s.proposed_employee_id in participant_ids else None
            review = bool(s.review_required) or proposed is None
            db.add(Speaker(
                meeting_id=meeting.id, recording_id=rec.id, label=s.label, proposed_employee_id=proposed,
                confirmed_employee_id=None if review else proposed, manually_set=False,
                similarity=s.similarity, second_similarity=s.second_similarity, review_required=review,
                review_reasons=list(s.review_reasons), clean_speech_seconds=float(s.clean_speech_seconds),
                utterance_count=counts.get(s.label, 0),
            ))
        outcome = result.extraction
        if outcome.status == "ok":
            _insert_tasks(db, meeting, outcome.tasks, id_map)
        meeting.summary = None
        meeting.summary_edited = False
        _apply_extraction(rec, meeting, outcome)
        rec.normalized_path = str(result.normalized_path)
        rec.duration_seconds = float(result.duration_seconds)
        rec.languages = list(result.languages)
        rec.processing_status = "done"
        rec.stage = "done"
        rec.error_code = rec.error_message = None
        meeting.draft_revision += 1
        notify_processing_result(db, meeting, rec)
        db.commit()
        return True


def _store_extraction(recording_id: uuid.UUID, generation: int, outcome) -> bool:
    with db_session() as db:
        rec = _lock_current(db, recording_id, generation)
        if rec is None:
            db.rollback()
            return False
        meeting = db.scalar(select(Meeting).where(Meeting.id == rec.meeting_id).with_for_update())
        assert meeting is not None
        if outcome.status == "ok":
            db.execute(delete(Task).where(Task.meeting_id == meeting.id, Task.status == "draft",
                                          Task.origin == "llm"))
            valid_ids = set(db.scalars(select(Utterance.id).where(Utterance.recording_id == rec.id)))
            _insert_tasks(db, meeting, outcome.tasks, {i: i for i in valid_ids})
        _apply_extraction(rec, meeting, outcome)
        rec.processing_status = "done"
        rec.stage = "done"
        rec.error_code = rec.error_message = None
        meeting.draft_revision += 1
        notify_processing_result(db, meeting, rec)
        db.commit()
        return True


# ------------------------------------------------------------------ job entry point


def run_recording_job(recording_id: uuid.UUID, generation: int, mode: str = "full") -> None:
    """BackgroundTasks entry point (runs in a worker thread, off the event loop)."""
    with jobs.lock:
        jobs.queued += 1
    sem = jobs.semaphore()
    sem.acquire()
    with jobs.lock:
        jobs.queued -= 1
        jobs.running += 1
    try:
        if not _is_current(recording_id, generation):
            return
        if mode == "extract":
            _run_extract(recording_id, generation)
        else:
            _run_full(recording_id, generation)
    except ai_gateway.AIUnavailable as exc:
        _mark_error(recording_id, generation, exc.code, exc.message)
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code.isupper():
            _mark_error(recording_id, generation, code, getattr(exc, "message", code), getattr(exc, "stage", None))
        else:
            log.error("recording job crashed: recording=%s type=%s", recording_id, type(exc).__name__)
            _mark_error(recording_id, generation, "INTERNAL_ERROR", "Внутренняя ошибка обработки")
    finally:
        with jobs.lock:
            jobs.running -= 1
        sem.release()


def _run_full(recording_id: uuid.UUID, generation: int) -> None:
    ai = ai_gateway.pipeline()
    T = ai_gateway.types()
    with db_session() as db:
        rec = db.get(Recording, recording_id)
        assert rec is not None
        meeting = db.get(Meeting, rec.meeting_id)
        assert meeting is not None
        work_dir = Path(rec.local_path).parent
        req = T.ProcessRequest(
            source_path=Path(rec.local_path), work_dir=work_dir,
            meeting=_meeting_context(db, meeting), candidates=_candidates(db, meeting),
            on_stage=lambda stage: _set_stage(recording_id, generation, stage),
            is_cancelled=lambda: not _is_current(recording_id, generation),
        )
    result = ai.process_recording(req)
    if not _store_full_result(recording_id, generation, result):
        log.info("stale recording result discarded: recording=%s generation=%s", recording_id, generation)


def _run_extract(recording_id: uuid.UUID, generation: int) -> None:
    ai = ai_gateway.pipeline()
    T = ai_gateway.types()
    with db_session() as db:
        rec = db.get(Recording, recording_id)
        assert rec is not None
        meeting = db.get(Meeting, rec.meeting_id)
        assert meeting is not None
        speakers = {s.label: s.confirmed_employee_id for s in rec.speakers}
        lines = [T.TranscriptLine(id=u.id, speaker_label=u.speaker_label,
                                  employee_id=speakers.get(u.speaker_label), text=u.text, start=u.start)
                 for u in db.scalars(select(Utterance).where(Utterance.recording_id == rec.id)
                                     .order_by(Utterance.start, Utterance.id))]
        ctx = _meeting_context(db, meeting)
    _set_stage(recording_id, generation, "extracting")
    outcome = ai.extract(lines, ctx)
    if not _store_extraction(recording_id, generation, outcome):
        log.info("stale extraction result discarded: recording=%s generation=%s", recording_id, generation)


# ------------------------------------------------------------------ restart recovery


def recover_interrupted(db: Session) -> int:
    """BackgroundTasks is not a durable queue: jobs cut by a restart become errors."""
    recs = db.scalars(select(Recording).where(Recording.processing_status == "processing").with_for_update()).all()
    for rec in recs:
        rec.processing_status = "error"
        rec.error_code = "INTERRUPTED"
        rec.error_message = "Обработка прервана перезапуском сервера. Нажмите «Повторить»."
        meeting = db.get(Meeting, rec.meeting_id)
        assert meeting is not None
        notify_processing_result(db, meeting, rec)
    db.commit()
    return len(recs)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
