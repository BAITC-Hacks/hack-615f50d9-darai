from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..access import (
    content_visible,
    get_meeting_for,
    get_meeting_for_edit,
    is_editor,
    parse_uuid,
    visible_meetings_filter,
)
from ..auth import CurrentUser, get_current_user
from ..db import get_db, session_factory
from ..errors import ApiError, conflict, forbidden, not_found, validation
from ..export import ExportData, ExportLine, ExportTask, build_docx, docx_to_pdf
from ..models import Employee, Meeting, MeetingParticipant, Recording, Speaker, Task, User, Utterance, utcnow
from ..notifications import invite_participants, notify_task_assignments
from ..processing import recording_dir, run_recording_job
from ..schemas import (
    ConfirmIn,
    MeetingCreate,
    MeetingDetailOut,
    MeetingListItemOut,
    MeetingPatch,
    Page,
    RecordingOut,
    SpeakersOut,
    SummaryOut,
    SummaryPatch,
    TaskCreate,
    TaskOut,
)
from ..serializers import employee_names, meeting_detail, meeting_list_item, recording_out, resolve_from, speaker_out, task_out
from ..uploads import check_audio, display_name, remove_tree, safe_suffix, save_upload

router = APIRouter(prefix="/meetings", tags=["meetings"])

SPEAKER_LABEL_PREFIX = "SPEAKER_"


# ------------------------------------------------------------------ helpers


def _to_utc(starts_at: datetime, tz: str) -> datetime:
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=ZoneInfo(tz))
    return starts_at.astimezone(timezone.utc)


def _active_employees(db: Session, ids: list[uuid.UUID]) -> list[Employee]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    # FOR SHARE: a concurrent archive (FOR UPDATE) waits or makes this check fail.
    found = db.scalars(select(Employee).where(Employee.id.in_(unique), Employee.active.is_(True))
                       .with_for_update(read=True)).all()
    if len(found) != len(unique):
        raise validation("Неизвестный или неактивный сотрудник в participant_ids", ["body", "participant_ids"])
    return list(found)


def _secretary(db: Session, secretary_id: uuid.UUID | None, current: CurrentUser) -> User:
    if secretary_id is None:
        return current.user
    user = db.get(User, secretary_id)
    if user is None or not user.active or user.role not in ("secretary", "admin"):
        raise validation("secretary_id должен быть активным пользователем с ролью secretary или admin",
                         ["body", "secretary_id"])
    return user


def _bump(meeting: Meeting) -> None:
    meeting.draft_revision += 1


def _create_meeting(db: Session, current: CurrentUser, data: MeetingCreate) -> Meeting:
    if current.role not in ("admin", "secretary"):
        raise forbidden("Создавать встречи может секретарь или администратор")
    employees = _active_employees(db, data.participant_ids)
    secretary = _secretary(db, data.secretary_id, current)
    meeting = Meeting(title=data.title.strip(), agenda=data.agenda, starts_at=_to_utc(data.starts_at, data.timezone),
                      timezone=data.timezone, organizer_id=current.id, secretary_id=secretary.id,
                      meeting_url=data.meeting_url)
    db.add(meeting)
    db.flush()
    for e in employees:
        db.add(MeetingParticipant(meeting_id=meeting.id, employee_id=e.id))
    db.flush()
    invite_participants(db, meeting, [e.id for e in employees])
    return meeting


def _store_recording(db: Session, meeting: Meeting, upload, background: BackgroundTasks) -> Recording:
    """Validate and store a new recording for a locked draft meeting, replacing the old one."""
    old = meeting.recording
    if old is not None and old.processing_status == "processing":
        raise conflict("RECORDING_PROCESSING", "Запись ещё обрабатывается")
    rec_id = uuid.uuid4()
    rdir = recording_dir(meeting.id, rec_id)
    source = rdir / f"source{safe_suffix(upload.filename)}"
    try:
        save_upload(upload, source)
        check_audio(source)
    except Exception:
        remove_tree(rdir)
        raise
    old_dir = None
    if old is not None:
        old_dir = Path(old.local_path).parent
        db.query(Task).filter(Task.meeting_id == meeting.id, Task.status == "draft").delete()
        db.delete(old)
        db.flush()
        meeting.summary = None
        meeting.summary_edited = False
    rec = Recording(id=rec_id, meeting_id=meeting.id, original_filename=display_name(upload.filename),
                    local_path=str(source), processing_status="processing", stage="queued", generation=1)
    db.add(rec)
    _bump(meeting)
    db.commit()
    if old_dir is not None:
        remove_tree(old_dir)
    background.add_task(run_recording_job, rec.id, rec.generation, "full")
    return rec


def _participant_ids(meeting: Meeting) -> set[uuid.UUID]:
    return {p.employee_id for p in meeting.participants}


# ------------------------------------------------------------------ meetings


@router.get("", response_model=Page[MeetingListItemOut])
def list_meetings(q: str | None = None,
                  approval_status: str | None = Query(None, pattern="^(draft|confirmed)$"),
                  processing_status: str | None = Query(None, pattern="^(processing|done|error)$"),
                  date_from: date | None = None, date_to: date | None = None,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = visible_meetings_filter(select(Meeting), current)
    if q and q.strip():
        stmt = stmt.where(Meeting.title.ilike(f"%{q.strip()}%"))
    if approval_status:
        stmt = stmt.where(Meeting.approval_status == approval_status)
    if processing_status:
        stmt = stmt.join(Recording, Recording.meeting_id == Meeting.id).where(
            Recording.processing_status == processing_status)
    if date_from:
        stmt = stmt.where(Meeting.starts_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc))
    if date_to:
        stmt = stmt.where(Meeting.starts_at <= datetime.combine(date_to, time.max, tzinfo=timezone.utc))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Meeting.starts_at.desc(), Meeting.id).limit(limit).offset(offset)).all()
    return Page[MeetingListItemOut](items=[meeting_list_item(db, m, current) for m in rows],
                                    total=total, limit=limit, offset=offset)


_MULTIPART_SCHEMA = {
    "type": "object",
    "required": ["file", "title", "starts_at", "timezone"],
    "properties": {
        "file": {"type": "string", "format": "binary"},
        "title": {"type": "string"},
        "starts_at": {"type": "string", "format": "date-time"},
        "timezone": {"type": "string"},
        "agenda": {"type": "string"},
        "participant_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}},
        "secretary_id": {"type": "string", "format": "uuid"},
        "meeting_url": {"type": "string", "format": "uri"},
    },
}


@router.post(
    "", status_code=201, response_model=MeetingDetailOut,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": MeetingCreate.model_json_schema()},
                "multipart/form-data": {"schema": _MULTIPART_SCHEMA},
            },
        },
        "responses": {"202": {"description": "Встреча создана с записью (multipart)",
                              "content": {"application/json": {
                                  "schema": {"$ref": "#/components/schemas/MeetingDetailOut"}}}}},
    },
)
async def create_meeting(request: Request, background: BackgroundTasks,
                         current: CurrentUser = Depends(get_current_user)):
    """JSON: meeting without recording (201). multipart: meeting + recording, processing starts (202)."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, StarletteUploadFile):
            raise validation("Поле file обязательно", ["body", "file"])
        raw = {k: form.get(k) for k in ("title", "starts_at", "timezone", "agenda", "secretary_id", "meeting_url")
               if form.get(k) not in (None, "")}
        raw["participant_ids"] = [v for v in form.getlist("participant_ids") + form.getlist("participant_ids[]") if v]
        data = _parse_meeting(raw)
        meeting_id = await run_in_threadpool(_create_with_recording, current, data, upload, background)
        status = 202
    else:
        try:
            raw = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "BAD_REQUEST", "Ожидался JSON или multipart/form-data")
        data = _parse_meeting(raw)
        meeting_id = await run_in_threadpool(_create_only, current, data)
        status = 201
    detail = await run_in_threadpool(_detail_by_id, meeting_id, current)
    return Response(content=detail.model_dump_json(), status_code=status, media_type="application/json")


def _parse_meeting(raw) -> MeetingCreate:
    try:
        return MeetingCreate.model_validate(raw)
    except ValidationError as exc:
        fields = [{"loc": ["body", *[str(p) for p in e["loc"]]], "msg": e["msg"]} for e in exc.errors()]
        raise ApiError(422, "VALIDATION_ERROR", "Некорректные данные встречи", {"fields": fields})


def _create_only(current: CurrentUser, data: MeetingCreate) -> uuid.UUID:
    with session_factory()() as db:
        current.user = db.merge(current.user)
        meeting = _create_meeting(db, current, data)
        db.commit()
        return meeting.id


def _create_with_recording(current: CurrentUser, data: MeetingCreate, upload, background) -> uuid.UUID:
    with session_factory()() as db:
        current.user = db.merge(current.user)
        meeting = _create_meeting(db, current, data)
        _store_recording(db, meeting, upload, background)
        return meeting.id


def _detail_by_id(meeting_id: uuid.UUID, current: CurrentUser) -> MeetingDetailOut:
    with session_factory()() as db:
        current.user = db.merge(current.user)
        return meeting_detail(db, get_meeting_for(db, meeting_id, current), current)


@router.get("/{meeting_id}", response_model=MeetingDetailOut)
def get_meeting(meeting_id: str, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return meeting_detail(db, get_meeting_for(db, meeting_id, current), current)


@router.patch("/{meeting_id}", response_model=MeetingDetailOut)
def patch_meeting(meeting_id: str, body: MeetingPatch, current: CurrentUser = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    fields = body.model_fields_set
    if "title" in fields and body.title is not None:
        meeting.title = body.title.strip()
    if "agenda" in fields:
        meeting.agenda = body.agenda or ""
    tz = body.timezone if "timezone" in fields and body.timezone else meeting.timezone
    if "starts_at" in fields and body.starts_at is not None:
        meeting.starts_at = _to_utc(body.starts_at, tz)
    elif tz != meeting.timezone:
        # keep the same wall-clock time when only the timezone is corrected
        local = meeting.starts_at.astimezone(ZoneInfo(meeting.timezone)).replace(tzinfo=None)
        meeting.starts_at = _to_utc(local, tz)
    meeting.timezone = tz
    if "secretary_id" in fields:
        meeting.secretary_id = _secretary(db, body.secretary_id, current).id
    if "participant_ids" in fields and body.participant_ids is not None:
        old_ids = _participant_ids(meeting)
        # already-invited (possibly archived) participants may stay; only additions must be active
        added = [i for i in dict.fromkeys(body.participant_ids) if i not in old_ids]
        _active_employees(db, added)
        new_ids = set(body.participant_ids)
        removed = old_ids - new_ids
        if removed:
            in_use = set()
            if meeting.recording is not None:
                in_use |= {s.confirmed_employee_id for s in meeting.recording.speakers}
            in_use |= {t.to_employee_id for t in meeting.tasks} | {t.from_employee_id for t in meeting.tasks}
            blocked = removed & in_use
            if blocked:
                raise conflict("PARTICIPANT_IN_USE", "Участник привязан к спикеру или поручению",
                               {"employee_ids": [str(i) for i in blocked]})
            for p in list(meeting.participants):
                if p.employee_id in removed:
                    meeting.participants.remove(p)
        for eid in new_ids - old_ids:
            meeting.participants.append(MeetingParticipant(meeting_id=meeting.id, employee_id=eid))
        db.flush()
        invite_participants(db, meeting, new_ids - old_ids)
    _bump(meeting)
    db.commit()
    return meeting_detail(db, meeting, current)


# ------------------------------------------------------------------ recordings


@router.post("/{meeting_id}/recordings", status_code=202, response_model=RecordingOut)
def upload_recording(meeting_id: str, background: BackgroundTasks, file: UploadFile = File(...),
                     current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    rec = _store_recording(db, meeting, file, background)
    return recording_out(rec)


def _get_recording(db: Session, meeting: Meeting, recording_id: str) -> Recording:
    rid = parse_uuid(recording_id, "Запись")
    rec = meeting.recording
    if rec is None or rec.id != rid:
        raise not_found("Запись")
    return rec


@router.get("/{meeting_id}/recordings/{recording_id}", response_model=RecordingOut)
def get_recording(meeting_id: str, recording_id: str, current: CurrentUser = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    meeting = get_meeting_for(db, meeting_id, current)
    if not is_editor(meeting, current):
        raise not_found("Запись")
    return recording_out(_get_recording(db, meeting, recording_id))


@router.post("/{meeting_id}/recordings/{recording_id}/retry", status_code=202, response_model=RecordingOut)
def retry_recording(meeting_id: str, recording_id: str, background: BackgroundTasks,
                    current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    rec = _get_recording(db, meeting, recording_id)
    db.refresh(rec, with_for_update=True)
    if rec.processing_status == "error":
        mode, stage = "full", "queued"
    elif rec.processing_status == "done" and rec.extraction_status == "error":
        mode, stage = "extract", "extracting"
    else:
        raise conflict("RETRY_NOT_ALLOWED", "Повтор возможен только после ошибки обработки или извлечения")
    rec.generation += 1
    rec.processing_status = "processing"
    rec.stage = stage
    rec.error_code = rec.error_message = None
    if mode == "extract":
        rec.extraction_status = "not_started"
        rec.extraction_error_code = rec.extraction_error_message = None
    db.commit()
    background.add_task(run_recording_job, rec.id, rec.generation, mode)
    return recording_out(rec)


@router.get("/{meeting_id}/recordings/{recording_id}/audio", response_class=FileResponse,
            responses={200: {"content": {"audio/wav": {}}}})
def recording_audio(meeting_id: str, recording_id: str, current: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    meeting = get_meeting_for(db, meeting_id, current)
    if not content_visible(meeting, current):
        raise not_found("Запись")
    rec = _get_recording(db, meeting, recording_id)
    if not rec.normalized_path or not Path(rec.normalized_path).is_file():
        raise not_found("Аудио")
    return FileResponse(rec.normalized_path, media_type="audio/wav",
                        headers={"Cache-Control": "private, no-store"})


# ------------------------------------------------------------------ review


def _ready_recording(meeting: Meeting) -> Recording:
    rec = meeting.recording
    if rec is None or rec.processing_status != "done":
        raise conflict("RECORDING_NOT_READY", "Нет обработанной записи")
    return rec


@router.patch("/{meeting_id}/speakers", response_model=SpeakersOut)
def patch_speakers(meeting_id: str, body: dict[str, uuid.UUID | None] = Body(
                       ..., examples=[{"SPEAKER_00": "5c1e0c0a-0000-0000-0000-000000000000", "SPEAKER_01": None}]),
                   current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    rec = _ready_recording(meeting)
    speakers = {s.label: s for s in rec.speakers}
    participants = _participant_ids(meeting)
    for label, emp_id in body.items():
        if label not in speakers:
            raise validation(f"Неизвестная метка спикера: {label}", ["body", label])
        if emp_id is not None and emp_id not in participants:
            raise validation("Сотрудник не является участником встречи", ["body", label])
    for label, emp_id in body.items():
        sp = speakers[label]
        sp.confirmed_employee_id = emp_id
        sp.manually_set = True
    _bump(meeting)
    db.commit()
    return SpeakersOut(speakers=[speaker_out(s) for s in rec.speakers], draft_revision=meeting.draft_revision)


@router.patch("/{meeting_id}/summary", response_model=SummaryOut)
def patch_summary(meeting_id: str, body: SummaryPatch, current: CurrentUser = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    meeting.summary = body.summary
    meeting.summary_edited = True
    _bump(meeting)
    db.commit()
    return SummaryOut(summary=meeting.summary, summary_edited=True, draft_revision=meeting.draft_revision)


def resolve_author_input(value: str | None, meeting: Meeting) -> tuple[uuid.UUID | None, str | None]:
    """'from' input -> (from_employee_id, from_label)."""
    if value is None:
        return None, None
    if value.startswith(SPEAKER_LABEL_PREFIX):
        labels = {s.label for s in meeting.recording.speakers} if meeting.recording else set()
        if value not in labels:
            raise validation("Неизвестная метка спикера в from", ["body", "from"])
        return None, value
    try:
        emp_id = uuid.UUID(value)
    except ValueError:
        raise validation("from: UUID участника, SPEAKER_XX или null", ["body", "from"])
    if emp_id not in _participant_ids(meeting):
        raise validation("Автор должен быть участником встречи", ["body", "from"])
    return emp_id, None


def validate_utterance_ids(db: Session, meeting: Meeting, ids: list[int]) -> list[int]:
    if not ids:
        return []
    if meeting.recording is None:
        raise validation("У встречи нет транскрипта", ["body", "source_utterance_ids"])
    found = set(db.scalars(select(Utterance.id).where(Utterance.recording_id == meeting.recording.id,
                                                      Utterance.id.in_(ids))))
    if found != set(ids):
        raise validation("Неизвестные source_utterance_ids", ["body", "source_utterance_ids"])
    return sorted(set(ids))


@router.post("/{meeting_id}/tasks", status_code=201, response_model=TaskOut)
def create_task(meeting_id: str, body: TaskCreate, current: CurrentUser = Depends(get_current_user),
                db: Session = Depends(get_db)):
    meeting = get_meeting_for_edit(db, meeting_id, current, lock=True)
    if body.to is not None and body.to not in _participant_ids(meeting):
        raise validation("Исполнитель должен быть участником встречи", ["body", "to"])
    from_emp, from_label = resolve_author_input(body.from_, meeting)
    reasons = []
    if body.to is None:
        reasons.append("missing_assignee")
    if body.deadline is None:
        reasons.append("missing_deadline")
    task = Task(meeting_id=meeting.id, from_employee_id=from_emp, from_label=from_label, to_employee_id=body.to,
                task=body.task.strip(), deadline=body.deadline, deadline_source=body.deadline_source,
                evidence=body.evidence, source_utterance_ids=validate_utterance_ids(db, meeting, body.source_utterance_ids),
                confidence=None, status="draft", execution_status="in_progress",
                needs_review=bool(reasons), review_reasons=reasons, origin="manual")
    db.add(task)
    _bump(meeting)
    db.commit()
    return task_out(db, task, meeting)


@router.post("/{meeting_id}/confirm", response_model=MeetingDetailOut)
def confirm_meeting(meeting_id: str, body: ConfirmIn, current: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    meeting = get_meeting_for(db, meeting_id, current, lock=True)
    if not is_editor(meeting, current):
        raise forbidden("Подтвердить протокол может организатор, секретарь или администратор")
    if meeting.approval_status == "confirmed":
        raise conflict("ALREADY_CONFIRMED", "Протокол уже подтверждён")
    if body.draft_revision != meeting.draft_revision:
        raise conflict("DRAFT_REVISION_MISMATCH", "Протокол изменился после загрузки страницы",
                       {"current_draft_revision": meeting.draft_revision})
    rec = _ready_recording(meeting)
    tasks = db.scalars(select(Task).where(Task.meeting_id == meeting.id).with_for_update()).all()
    missing = {
        "tasks_without_assignee": [str(t.id) for t in tasks if t.to_employee_id is None],
        "tasks_without_deadline": [str(t.id) for t in tasks if t.deadline is None],
        "unresolved_speakers": [s.label for s in rec.speakers
                                if s.confirmed_employee_id is None and s.utterance_count > 0],
        "extraction_error": rec.extraction_status == "error",
    }
    incomplete = any(missing[k] for k in missing)
    if incomplete and not body.acknowledge_incomplete:
        raise conflict("CONFIRMATION_REQUIRES_ACKNOWLEDGEMENT", "Есть незаполненные поля", missing)
    now = utcnow()
    for t in tasks:
        t.status = "confirmed"
    meeting.approval_status = "confirmed"
    meeting.protocol_version += 1
    meeting.confirmed_at = now
    meeting.confirmed_by = current.id
    db.flush()
    notify_task_assignments(db, meeting, list(tasks))
    db.commit()
    return meeting_detail(db, meeting, current)


# ------------------------------------------------------------------ export


def _export_data(db: Session, meeting: Meeting, include_transcript: bool) -> ExportData:
    speakers = {s.label: s for s in meeting.recording.speakers} if meeting.recording else {}
    tasks = db.scalars(select(Task).where(Task.meeting_id == meeting.id).order_by(Task.created_at, Task.id)).all()
    authors = [resolve_from(t, speakers) for t in tasks]
    ids = {a for a in authors if isinstance(a, uuid.UUID)} | {t.to_employee_id for t in tasks} | \
        {s.confirmed_employee_id for s in speakers.values()}
    names = employee_names(db, ids)

    def name(v) -> str:
        if isinstance(v, uuid.UUID):
            return names.get(v, "—")
        return v or "не указан"

    export_tasks = [
        ExportTask(number=i, task=t.task, author=name(a), assignee=name(t.to_employee_id),
                   deadline=t.deadline, deadline_source=t.deadline_source, evidence=t.evidence,
                   execution_status=t.execution_status)
        for i, (t, a) in enumerate(zip(tasks, authors), 1)
    ]
    transcript = None
    if include_transcript and meeting.recording is not None:
        transcript = []
        for u in db.scalars(select(Utterance).where(Utterance.recording_id == meeting.recording.id)
                            .order_by(Utterance.start, Utterance.id)):
            sp = speakers.get(u.speaker_label) if u.speaker_label else None
            who = names.get(sp.confirmed_employee_id) if sp and sp.confirmed_employee_id else None
            transcript.append(ExportLine(start=u.start, speaker=who or u.speaker_label or "Неизвестный", text=u.text))
    participants = sorted(((p.employee.fio, p.employee.position) for p in meeting.participants))
    return ExportData(title=meeting.title, starts_at=meeting.starts_at, timezone=meeting.timezone,
                      agenda=meeting.agenda, participants=participants, summary=meeting.summary,
                      tasks=export_tasks, transcript=transcript, draft=meeting.approval_status != "confirmed",
                      protocol_version=meeting.protocol_version, confirmed_at=meeting.confirmed_at)


@router.get("/{meeting_id}/export", response_class=Response,
            responses={200: {"content": {"application/pdf": {},
                                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {}}}})
def export_meeting(meeting_id: str, fmt: str = Query(..., pattern="^(docx|pdf)$"), include_transcript: bool = False,
                   current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    meeting = get_meeting_for(db, meeting_id, current)
    if not content_visible(meeting, current):
        raise not_found("Протокол")
    data = _export_data(db, meeting, include_transcript)
    content = build_docx(data)
    if fmt == "pdf":
        content = docx_to_pdf(content)
        media = "application/pdf"
    else:
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    local_date = meeting.starts_at.astimezone(ZoneInfo(meeting.timezone)).strftime("%Y-%m-%d")
    filename = f"protocol_{local_date}{'_draft' if data.draft else ''}.{fmt}"
    return Response(content=content, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    })
