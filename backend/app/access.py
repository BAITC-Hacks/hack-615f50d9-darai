"""Server-side access rules for meetings and tasks.

Inaccessible objects are reported as 404 so that guessed IDs reveal nothing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.orm import Session

from .auth import CurrentUser
from .errors import ApiError, not_found
from .models import Meeting, MeetingParticipant, Task


def visible_meetings_filter(stmt: Select, current: CurrentUser) -> Select:
    if current.is_admin:
        return stmt
    conditions = [Meeting.organizer_id == current.id, Meeting.secretary_id == current.id]
    if current.employee_id is not None:
        conditions.append(
            exists().where(
                MeetingParticipant.meeting_id == Meeting.id,
                MeetingParticipant.employee_id == current.employee_id,
            )
        )
    return stmt.where(or_(*conditions))


def is_editor(meeting: Meeting, current: CurrentUser) -> bool:
    return current.is_admin or meeting.organizer_id == current.id or meeting.secretary_id == current.id


def is_participant(meeting: Meeting, current: CurrentUser) -> bool:
    return current.employee_id is not None and any(
        p.employee_id == current.employee_id for p in meeting.participants
    )


def can_view(meeting: Meeting, current: CurrentUser) -> bool:
    return is_editor(meeting, current) or is_participant(meeting, current)


def content_visible(meeting: Meeting, current: CurrentUser) -> bool:
    """Draft content is for editors only; participants see the confirmed protocol."""
    return is_editor(meeting, current) or (
        meeting.approval_status == "confirmed" and is_participant(meeting, current)
    )


def parse_uuid(value: str, what: str = "Объект") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        raise not_found(what)


def get_meeting_for(db: Session, meeting_id, current: CurrentUser, *, lock: bool = False) -> Meeting:
    mid = parse_uuid(meeting_id, "Встреча")
    stmt = select(Meeting).where(Meeting.id == mid)
    if lock:
        stmt = stmt.with_for_update()
    meeting = db.scalar(stmt)
    if meeting is None or not can_view(meeting, current):
        raise not_found("Встреча")
    return meeting


def get_meeting_for_edit(db: Session, meeting_id, current: CurrentUser, *, lock: bool = False,
                         require_draft: bool = True) -> Meeting:
    meeting = get_meeting_for(db, meeting_id, current, lock=lock)
    if not is_editor(meeting, current):
        raise ApiError(403, "FORBIDDEN", "Редактировать встречу может только организатор, секретарь или администратор")
    if require_draft and meeting.approval_status != "draft":
        raise ApiError(409, "MEETING_CONFIRMED", "Протокол уже подтверждён, изменения запрещены")
    return meeting


def get_task_for(db: Session, task_id, current: CurrentUser, *, lock: bool = False) -> tuple[Task, Meeting]:
    tid = parse_uuid(task_id, "Поручение")
    stmt = select(Task).where(Task.id == tid)
    if lock:
        stmt = stmt.with_for_update()
    task = db.scalar(stmt)
    if task is None:
        raise not_found("Поручение")
    meeting = db.get(Meeting, task.meeting_id)
    assert meeting is not None
    editor = is_editor(meeting, current)
    assignee = (
        task.status == "confirmed"
        and current.employee_id is not None
        and task.to_employee_id == current.employee_id
    )
    participant_view = task.status == "confirmed" and is_participant(meeting, current)
    if not (editor or assignee or participant_view):
        raise not_found("Поручение")
    return task, meeting
