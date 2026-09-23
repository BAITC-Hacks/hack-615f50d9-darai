"""In-CRM notifications and deadline reminders.

Every event carries a unique dedup_key (recipient + event + object + deadline
version / generation) and is inserted with ON CONFLICT DO NOTHING, so repeated
confirms, reminder ticks and restarts never create duplicates.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import Meeting, Notification, Recording, Task, User


def deadline_at(deadline: date | None, tz_name: str) -> datetime | None:
    """End of the deadline day in the meeting's timezone, as UTC."""
    if deadline is None:
        return None
    local = datetime.combine(deadline, time(23, 59, 59), tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def is_overdue(task: Task, tz_name: str, now: datetime) -> bool:
    due = deadline_at(task.deadline, tz_name)
    return (
        task.status == "confirmed"
        and task.execution_status != "completed"
        and due is not None
        and now > due
    )


def notify(db: Session, *, recipient_user_id: uuid.UUID, event_type: str, title: str, message: str,
           dedup_key: str, meeting_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None) -> bool:
    stmt = (
        insert(Notification)
        .values(id=uuid.uuid4(), recipient_user_id=recipient_user_id, event_type=event_type,
                title=title[:255], message=message, meeting_id=meeting_id, task_id=task_id,
                dedup_key=dedup_key[:255], created_at=datetime.now(timezone.utc))
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(Notification.id)
    )
    return db.execute(stmt).scalar() is not None


def users_by_employee(db: Session, employee_ids) -> dict[uuid.UUID, User]:
    ids = [e for e in employee_ids if e is not None]
    if not ids:
        return {}
    users = db.scalars(select(User).where(User.employee_id.in_(ids), User.active.is_(True))).all()
    return {u.employee_id: u for u in users}


def _fmt_local(dt: datetime, tz_name: str) -> str:
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M")


def invite_participants(db: Session, meeting: Meeting, employee_ids) -> int:
    created = 0
    when = _fmt_local(meeting.starts_at, meeting.timezone)
    for user in users_by_employee(db, employee_ids).values():
        created += notify(
            db, recipient_user_id=user.id, event_type="meeting_invitation",
            title="Приглашение на совещание",
            message=f"{meeting.title} — {when} ({meeting.timezone}). Совещание будет записано и обработано ИИ локально.",
            meeting_id=meeting.id, dedup_key=f"invite:{meeting.id}:{user.id}",
        )
    return created


def _editors(meeting: Meeting) -> set[uuid.UUID]:
    return {meeting.organizer_id, meeting.secretary_id}


def notify_processing_result(db: Session, meeting: Meeting, recording: Recording) -> int:
    created = 0
    failed = recording.processing_status == "error" or recording.extraction_status == "error"
    for user_id in _editors(meeting):
        if failed:
            what = recording.error_code or recording.extraction_error_code or "ERROR"
            created += notify(
                db, recipient_user_id=user_id, event_type="processing_failed",
                title="Ошибка обработки записи",
                message=f"{meeting.title}: {what}. Откройте встречу, чтобы повторить обработку.",
                meeting_id=meeting.id,
                dedup_key=f"failed:{recording.id}:{recording.generation}:{user_id}",
            )
        else:
            created += notify(
                db, recipient_user_id=user_id, event_type="protocol_ready",
                title="Протокол готов к проверке",
                message=f"{meeting.title}: транскрипт и черновик поручений готовы.",
                meeting_id=meeting.id,
                dedup_key=f"ready:{recording.id}:{recording.generation}:{user_id}",
            )
    return created


def notify_task_assignments(db: Session, meeting: Meeting, tasks: list[Task]) -> int:
    """Called inside the confirm transaction: only confirmed tasks reach assignees."""
    created = 0
    users = users_by_employee(db, {t.to_employee_id for t in tasks})
    for task in tasks:
        if task.status != "confirmed" or task.to_employee_id is None:
            continue
        user = users.get(task.to_employee_id)
        if user is None:
            continue
        due = f" — срок {task.deadline.isoformat()}" if task.deadline else " — срок не указан"
        created += notify(
            db, recipient_user_id=user.id, event_type="task_assigned",
            title="Вам назначено поручение", message=f"{task.task}{due}",
            meeting_id=meeting.id, task_id=task.id,
            dedup_key=f"assigned:{task.id}:{user.id}:v{meeting.protocol_version}",
        )
    return created


def run_reminders(db: Session, now: datetime, lead_hours: int) -> int:
    """One scheduler tick. Safe to repeat: dedup keys include the deadline value."""
    lead = timedelta(hours=lead_hours)
    horizon = (now + lead + timedelta(days=2)).date()
    rows = db.execute(
        select(Task, Meeting)
        .join(Meeting, Meeting.id == Task.meeting_id)
        .where(Task.status == "confirmed", Task.execution_status == "in_progress",
               Task.deadline.is_not(None), Task.deadline <= horizon)
    ).all()
    created = 0
    for task, meeting in rows:
        due = deadline_at(task.deadline, meeting.timezone)
        assert due is not None
        if now > due:
            event, title = "task_overdue", "Поручение просрочено"
        elif due - now <= lead:
            event, title = "deadline_soon", "Скоро срок поручения"
        else:
            continue
        recipients: set[uuid.UUID] = {meeting.organizer_id}
        assignee = users_by_employee(db, [task.to_employee_id]).get(task.to_employee_id)
        if assignee is not None:
            recipients.add(assignee.id)
        prefix = "overdue" if event == "task_overdue" else "soon"
        for user_id in recipients:
            created += notify(
                db, recipient_user_id=user_id, event_type=event, title=title,
                message=f"{task.task} — срок {task.deadline.isoformat()}",
                meeting_id=meeting.id, task_id=task.id,
                dedup_key=f"{prefix}:{task.id}:{task.deadline.isoformat()}:{user_id}",
            )
    db.commit()
    return created
