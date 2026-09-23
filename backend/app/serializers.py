"""ORM -> response models. Never exposes voice vectors."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import ai_gateway
from .access import content_visible, is_editor
from .auth import CurrentUser
from .models import Employee, Meeting, MeetingParticipant, Recording, Speaker, Task, User, Utterance, VoiceProfile
from .notifications import deadline_at, is_overdue
from .schemas import (
    EmployeeOut,
    ExtractionOut,
    MeetingDetailOut,
    MeetingListItemOut,
    ParticipantOut,
    Permissions,
    RecordingOut,
    SpeakerOut,
    TaskOut,
    UserRef,
    UtteranceOut,
    VoiceStatusOut,
)


def profile_compatible(profile: VoiceProfile, info=None) -> bool:
    info = info or ai_gateway.voice_model_info()
    return (profile.model_id == info.model_id and profile.model_revision == info.revision
            and profile.dimension == info.dimension)


def voice_status(profile: VoiceProfile | None, info=None) -> VoiceStatusOut:
    if profile is None:
        return VoiceStatusOut(status="none")
    if not profile_compatible(profile, info):
        status = "incompatible"
    else:
        status = "needs_review" if profile.quality_status == "needs_review" else "ok"
    return VoiceStatusOut(status=status, quality_status=profile.quality_status,
                          speech_seconds=round(profile.speech_seconds, 1),
                          created_at=profile.created_at, consent_at=profile.consent_at)


def active_admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User)
                     .where(User.role == "admin", User.active.is_(True))) or 0


def delete_denial(emp: Employee, current: CurrentUser | None, active_admins: int) -> tuple[int, str, str] | None:
    """Why `current` may not archive `emp` (status, code, message), or None if allowed."""
    if current is None or current.role not in ("admin", "secretary"):
        return 403, "FORBIDDEN", "Удалять сотрудников может администратор или секретарь"
    if current.employee_id == emp.id:
        return 409, "SELF_DELETE_FORBIDDEN", "Нельзя удалить собственную карточку"
    target = emp.user
    if current.role == "secretary" and target is not None and target.role in ("secretary", "admin"):
        return 403, "FORBIDDEN", "Секретарь может удалять только сотрудников без учётной записи или с ролью employee"
    if target is not None and target.role == "admin" and target.active and active_admins <= 1:
        return 409, "LAST_ADMIN", "Нельзя удалить последнего активного администратора"
    return None


def employee_out(emp: Employee, info=None, current: CurrentUser | None = None,
                 active_admins: int | None = None) -> EmployeeOut:
    can_delete = False
    if current is not None and emp.active:
        admins = active_admins if active_admins is not None else 2
        can_delete = delete_denial(emp, current, admins) is None
    return EmployeeOut(
        id=emp.id, fio=emp.fio, position=emp.position, department=emp.department, active=emp.active,
        has_account=emp.user is not None and emp.user.active,
        user_id=emp.user.id if emp.user is not None else None,
        can_delete=can_delete,
        voice_profile=voice_status(emp.voice_profile, info),
    )


def user_ref(user: User) -> UserRef:
    return UserRef(id=user.id, login=user.login, employee_id=user.employee_id,
                   fio=user.employee.fio if user.employee else None)


def recording_out(rec: Recording) -> RecordingOut:
    return RecordingOut(
        id=rec.id, meeting_id=rec.meeting_id, processing_status=rec.processing_status, stage=rec.stage,
        error_code=rec.error_code, error_message=rec.error_message, generation=rec.generation,
        original_filename=rec.original_filename, duration_seconds=rec.duration_seconds,
        languages=list(rec.languages or []),
        extraction=ExtractionOut(status=rec.extraction_status, error_code=rec.extraction_error_code,
                                 error_message=rec.extraction_error_message),
        audio_url=(f"/meetings/{rec.meeting_id}/recordings/{rec.id}/audio" if rec.normalized_path else None),
        created_at=rec.created_at, updated_at=rec.updated_at,
    )


def speaker_out(sp: Speaker) -> SpeakerOut:
    return SpeakerOut(
        label=sp.label, proposed_employee_id=sp.proposed_employee_id,
        confirmed_employee_id=sp.confirmed_employee_id, manually_set=sp.manually_set,
        similarity=sp.similarity, second_similarity=sp.second_similarity,
        review_required=sp.review_required, review_reasons=list(sp.review_reasons or []),
        clean_speech_seconds=round(sp.clean_speech_seconds, 2), utterance_count=sp.utterance_count,
    )


def resolve_from(task: Task, speakers: dict[str, Speaker]) -> uuid.UUID | str | None:
    if task.from_employee_id is not None:
        return task.from_employee_id
    if task.from_label:
        sp = speakers.get(task.from_label)
        if sp is not None and sp.confirmed_employee_id is not None:
            return sp.confirmed_employee_id
        return task.from_label
    return None


def meeting_speakers(db: Session, meeting: Meeting) -> dict[str, Speaker]:
    if meeting.recording is None:
        return {}
    return {s.label: s for s in meeting.recording.speakers}


def employee_names(db: Session, ids) -> dict[uuid.UUID, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return dict(db.execute(select(Employee.id, Employee.fio).where(Employee.id.in_(ids))).all())


def tasks_out(db: Session, rows: list[tuple[Task, Meeting]], now: datetime | None = None) -> list[TaskOut]:
    now = now or datetime.now(timezone.utc)
    speakers_cache: dict[uuid.UUID, dict[str, Speaker]] = {}
    resolved = []
    ids: set = set()
    for task, meeting in rows:
        if meeting.id not in speakers_cache:
            speakers_cache[meeting.id] = meeting_speakers(db, meeting)
        frm = resolve_from(task, speakers_cache[meeting.id])
        resolved.append(frm)
        if isinstance(frm, uuid.UUID):
            ids.add(frm)
        ids.add(task.to_employee_id)
    names = employee_names(db, ids)
    out = []
    for (task, meeting), frm in zip(rows, resolved):
        out.append(TaskOut(
            id=task.id, meeting_id=meeting.id, meeting_title=meeting.title,
            **{"from": str(frm) if frm is not None else None},
            from_speaker_label=task.from_label,
            from_fio=names.get(frm) if isinstance(frm, uuid.UUID) else None,
            to=task.to_employee_id, to_fio=names.get(task.to_employee_id),
            task=task.task, deadline=task.deadline, deadline_source=task.deadline_source,
            deadline_at=deadline_at(task.deadline, meeting.timezone), evidence=task.evidence,
            source_utterance_ids=list(task.source_utterance_ids or []), confidence=task.confidence,
            status=task.status, execution_status=task.execution_status, completed_at=task.completed_at,
            overdue=is_overdue(task, meeting.timezone, now), needs_review=task.needs_review,
            review_reasons=list(task.review_reasons or []), origin=task.origin,
            created_at=task.created_at, updated_at=task.updated_at,
        ))
    return out


def task_out(db: Session, task: Task, meeting: Meeting) -> TaskOut:
    return tasks_out(db, [(task, meeting)])[0]


def _participant_count(db: Session, meeting_id) -> int:
    return db.scalar(select(func.count()).select_from(MeetingParticipant)
                     .where(MeetingParticipant.meeting_id == meeting_id)) or 0


def meeting_list_item(db: Session, meeting: Meeting, current: CurrentUser) -> MeetingListItemOut:
    return MeetingListItemOut(
        id=meeting.id, title=meeting.title, starts_at=meeting.starts_at, timezone=meeting.timezone,
        organizer=user_ref(meeting.organizer), secretary=user_ref(meeting.secretary),
        approval_status=meeting.approval_status, protocol_version=meeting.protocol_version,
        participant_count=_participant_count(db, meeting.id),
        recording=recording_out(meeting.recording) if meeting.recording else None,
        can_edit=is_editor(meeting, current) and meeting.approval_status == "draft",
        meeting_url=meeting.meeting_url,
    )


def meeting_detail(db: Session, meeting: Meeting, current: CurrentUser) -> MeetingDetailOut:
    db.refresh(meeting)
    base = meeting_list_item(db, meeting, current)
    editor = is_editor(meeting, current)
    visible = content_visible(meeting, current)
    info = ai_gateway.voice_model_info()
    participants = []
    for p in sorted(meeting.participants, key=lambda p: p.employee.fio):
        e = p.employee
        participants.append(ParticipantOut(
            employee_id=e.id, fio=e.fio, position=e.position, department=e.department,
            has_account=e.user is not None and e.user.active,
            voice_status=voice_status(e.voice_profile, info).status,
        ))
    speakers: list = []
    utterances: list = []
    tasks: list = []
    summary = None
    if visible:
        rec = meeting.recording
        if rec is not None:
            sp_map = {s.label: s for s in rec.speakers}
            speakers = [speaker_out(s) for s in rec.speakers]
            utterances = [
                UtteranceOut(
                    id=u.id, speaker_label=u.speaker_label,
                    employee_id=sp_map[u.speaker_label].confirmed_employee_id
                    if u.speaker_label in sp_map else None,
                    start=u.start, end=u.end, text=u.text, language=u.language,
                    uncertain=u.uncertain, uncertain_reasons=list(u.uncertain_reasons or []),
                )
                for u in db.scalars(select(Utterance).where(Utterance.recording_id == rec.id)
                                    .order_by(Utterance.start, Utterance.id))
            ]
        task_rows = db.scalars(select(Task).where(Task.meeting_id == meeting.id)
                               .order_by(Task.created_at, Task.id)).all()
        tasks = tasks_out(db, [(t, meeting) for t in task_rows])
        summary = meeting.summary
    draft = meeting.approval_status == "draft"
    return MeetingDetailOut(
        **base.model_dump(),
        agenda=meeting.agenda, draft_revision=meeting.draft_revision,
        confirmed_at=meeting.confirmed_at, confirmed_by=meeting.confirmed_by,
        participants=participants,
        permissions=Permissions(can_edit=editor and draft, can_confirm=editor and draft,
                                can_export_draft=editor),
        content_visible=visible, speakers=speakers, utterances=utterances,
        summary=summary, summary_edited=meeting.summary_edited, tasks=tasks,
    )
