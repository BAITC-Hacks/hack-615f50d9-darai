from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..access import get_task_for, is_editor, parse_uuid, visible_meetings_filter
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..errors import ApiError, conflict, forbidden, validation
from ..models import Meeting, Task, utcnow
from ..notifications import is_overdue
from ..schemas import Page, TaskOut, TaskPatch
from ..serializers import task_out, tasks_out
from .meetings import resolve_author_input, validate_utterance_ids

router = APIRouter(prefix="/tasks", tags=["tasks"])

_EDIT_FIELDS = {"task", "to", "from_", "deadline", "deadline_source", "evidence", "source_utterance_ids", "needs_review"}
_RECOMPUTED = {"missing_assignee", "missing_deadline", "deadline_without_source", "unresolved_author"}


@router.get("", response_model=Page[TaskOut])
def list_tasks(assignee: str = "me", meeting_id: str | None = None,
               execution_status: str | None = Query(None, pattern="^(in_progress|completed)$"),
               overdue: bool | None = None,
               limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """Confirmed tasks only; drafts are visible to editors through GET /meetings/{id}."""
    if assignee == "me":
        employee_id = current.employee_id
    else:
        if not current.is_admin:
            raise ApiError(403, "FORBIDDEN", "Чужие поручения доступны только администратору")
        employee_id = parse_uuid(assignee, "Сотрудник")
    if employee_id is None:
        return Page[TaskOut](items=[], total=0, limit=limit, offset=offset)
    stmt = (select(Task, Meeting).join(Meeting, Meeting.id == Task.meeting_id)
            .where(Task.status == "confirmed", Task.to_employee_id == employee_id))
    if assignee != "me":
        stmt = visible_meetings_filter(stmt, current)
    if meeting_id:
        stmt = stmt.where(Task.meeting_id == parse_uuid(meeting_id, "Встреча"))
    if execution_status:
        stmt = stmt.where(Task.execution_status == execution_status)
    rows = db.execute(stmt.order_by(Task.deadline.asc().nulls_last(), Task.created_at, Task.id)).all()
    now = datetime.now(timezone.utc)
    if overdue is not None:
        rows = [r for r in rows if is_overdue(r[0], r[1].timezone, now) == overdue]
    page = rows[offset:offset + limit]
    return Page[TaskOut](items=tasks_out(db, [(t, m) for t, m in page], now), total=len(rows),
                         limit=limit, offset=offset)


@router.patch("/{task_id}", response_model=TaskOut)
def patch_task(task_id: str, body: TaskPatch, current: CurrentUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    task, meeting = get_task_for(db, task_id, current, lock=True)
    fields = set(body.model_fields_set)
    edit = fields & _EDIT_FIELDS
    execution = "execution_status" in fields
    if edit and execution:
        raise forbidden("Нельзя одновременно править поручение и статус исполнения")
    editor = is_editor(meeting, current)
    if execution:
        if body.execution_status is None:
            raise validation("execution_status не может быть null", ["body", "execution_status"])
        assignee = current.employee_id is not None and task.to_employee_id == current.employee_id
        if not (assignee or editor):
            raise forbidden("Статус исполнения меняет исполнитель")
        if task.status != "confirmed":
            raise conflict("TASK_NOT_CONFIRMED", "Поручение ещё не подтверждено")
        task.execution_status = body.execution_status
        task.completed_at = utcnow() if body.execution_status == "completed" else None
        db.commit()
        return task_out(db, task, meeting)
    if not edit:
        return task_out(db, task, meeting)
    if not editor:
        raise forbidden("Править поручение может организатор, секретарь или администратор")
    if meeting.approval_status != "draft":
        raise conflict("MEETING_CONFIRMED", "Протокол уже подтверждён, изменения запрещены")
    if "task" in fields:
        if not body.task or not body.task.strip():
            raise validation("task не может быть пустым", ["body", "task"])
        task.task = body.task.strip()
    if "to" in fields:
        if body.to is not None and body.to not in {p.employee_id for p in meeting.participants}:
            raise validation("Исполнитель должен быть участником встречи", ["body", "to"])
        task.to_employee_id = body.to
    if "from_" in fields:
        task.from_employee_id, task.from_label = resolve_author_input(body.from_, meeting)
    if "deadline" in fields:
        task.deadline = body.deadline
    if "deadline_source" in fields:
        task.deadline_source = body.deadline_source
    if "evidence" in fields:
        task.evidence = body.evidence
    if "source_utterance_ids" in fields:
        task.source_utterance_ids = validate_utterance_ids(db, meeting, body.source_utterance_ids or [])
    reasons = [r for r in (task.review_reasons or []) if r not in _RECOMPUTED]
    if task.to_employee_id is None:
        reasons.append("missing_assignee")
    if task.deadline is None:
        reasons.append("missing_deadline")
    if task.from_employee_id is None and task.from_label is None and task.origin == "llm":
        reasons.append("unresolved_author")
    task.review_reasons = reasons
    task.needs_review = body.needs_review if "needs_review" in fields and body.needs_review is not None \
        else bool(reasons)
    meeting.draft_revision += 1
    db.commit()
    return task_out(db, task, meeting)


@router.delete("/{task_id}", status_code=204, response_class=Response)
def delete_task(task_id: str, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    task, meeting = get_task_for(db, task_id, current, lock=True)
    if not is_editor(meeting, current):
        raise forbidden("Удалить поручение может организатор, секретарь или администратор")
    if meeting.approval_status != "draft":
        raise conflict("MEETING_CONFIRMED", "Протокол уже подтверждён, изменения запрещены")
    db.delete(task)
    meeting.draft_revision += 1
    db.commit()
    return Response(status_code=204)
