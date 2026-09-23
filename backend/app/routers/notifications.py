from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..access import parse_uuid
from ..auth import CurrentUser, get_current_user
from ..db import get_db
from ..errors import not_found
from ..models import Notification, utcnow
from ..schemas import NotificationOut, NotificationPatch, NotificationsPage, ReadAllOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _out(n: Notification) -> NotificationOut:
    return NotificationOut(id=n.id, event_type=n.event_type, title=n.title, message=n.message,
                           meeting_id=n.meeting_id, task_id=n.task_id, created_at=n.created_at, read_at=n.read_at)


@router.get("", response_model=NotificationsPage)
def list_notifications(unread_only: bool = False, limit: int = Query(50, ge=1, le=200),
                       offset: int = Query(0, ge=0), current: CurrentUser = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    mine = Notification.recipient_user_id == current.id
    stmt = select(Notification).where(mine)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    unread = db.scalar(select(func.count()).select_from(Notification)
                       .where(mine, Notification.read_at.is_(None))) or 0
    rows = db.scalars(stmt.order_by(Notification.created_at.desc(), Notification.id).limit(limit).offset(offset)).all()
    return NotificationsPage(items=[_out(n) for n in rows], total=total, limit=limit, offset=offset,
                             unread_count=unread)


@router.patch("/{notification_id}", response_model=NotificationOut)
def patch_notification(notification_id: str, body: NotificationPatch,
                       current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, parse_uuid(notification_id, "Уведомление"))
    if n is None or n.recipient_user_id != current.id:
        raise not_found("Уведомление")
    n.read_at = (n.read_at or utcnow()) if body.read else None
    db.commit()
    return _out(n)


@router.post("/read-all", response_model=ReadAllOut)
def read_all(current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    res = db.execute(update(Notification)
                     .where(Notification.recipient_user_id == current.id, Notification.read_at.is_(None))
                     .values(read_at=utcnow()))
    db.commit()
    return ReadAllOut(updated=res.rowcount)
