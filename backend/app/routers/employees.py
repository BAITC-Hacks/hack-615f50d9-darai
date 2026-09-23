from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import ai_gateway
from ..access import parse_uuid
from ..auth import CurrentUser, get_current_user, require_admin
from ..config import get_settings
from ..db import get_db
from ..errors import ApiError, forbidden, not_found
from ..models import Employee, VoiceProfile, utcnow
from ..schemas import EmployeeCreate, EmployeeOut, EmployeePatch, Page, VoiceEnrollmentOut
from ..serializers import employee_out
from ..uploads import check_audio, remove_tree, safe_suffix, save_upload

log = logging.getLogger("darai.voice")
router = APIRouter(prefix="/employees", tags=["employees"])


def _get(db: Session, employee_id: str) -> Employee:
    emp = db.scalar(select(Employee).where(Employee.id == parse_uuid(employee_id, "Сотрудник"))
                    .options(selectinload(Employee.user), selectinload(Employee.voice_profile)))
    if emp is None:
        raise not_found("Сотрудник")
    return emp


@router.get("", response_model=Page[EmployeeOut])
def list_employees(q: str | None = None, department: str | None = None,
                   active: str = Query("true", pattern="^(true|false|all)$"),
                   limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   _: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    stmt = select(Employee)
    if active != "all":
        stmt = stmt.where(Employee.active.is_(active == "true"))
    if department:
        stmt = stmt.where(Employee.department == department)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Employee.fio.ilike(like), Employee.position.ilike(like),
                              Employee.department.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.options(selectinload(Employee.user), selectinload(Employee.voice_profile))
                      .order_by(Employee.fio, Employee.id).limit(limit).offset(offset)).all()
    info = ai_gateway.voice_model_info()
    return Page[EmployeeOut](items=[employee_out(e, info) for e in rows], total=total, limit=limit, offset=offset)


@router.get("/{employee_id}", response_model=EmployeeOut)
def get_employee(employee_id: str, _: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return employee_out(_get(db, employee_id))


@router.post("", response_model=EmployeeOut, status_code=201)
def create_employee(body: EmployeeCreate, _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    emp = Employee(fio=body.fio.strip(), position=body.position.strip(), department=body.department.strip())
    db.add(emp)
    db.commit()
    return employee_out(_get(db, str(emp.id)))


@router.patch("/{employee_id}", response_model=EmployeeOut)
def patch_employee(employee_id: str, body: EmployeePatch, _: CurrentUser = Depends(require_admin),
                   db: Session = Depends(get_db)):
    emp = _get(db, employee_id)
    for field in ("fio", "position", "department"):
        value = getattr(body, field)
        if value is not None:
            setattr(emp, field, value.strip())
    if body.active is not None:
        emp.active = body.active
    db.commit()
    return employee_out(_get(db, employee_id))


def _check_voice_access(emp: Employee, current: CurrentUser) -> None:
    if not (current.is_admin or current.employee_id == emp.id):
        raise forbidden("Голосовой профиль может менять только сам сотрудник или администратор")


@router.post("/{employee_id}/voice", response_model=VoiceEnrollmentOut, status_code=201)
def enroll_voice(employee_id: str, file: UploadFile = File(...), consent: str = Form(...),
                 current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _get(db, employee_id)
    _check_voice_access(emp, current)
    if consent.strip().lower() != "true":
        raise ApiError(422, "VALIDATION_ERROR", "Требуется согласие на обработку голосового профиля",
                       {"fields": [{"loc": ["body", "consent"], "msg": "must be true"}]})
    settings = get_settings()
    work_dir = settings.tmp_dir / f"voice-{uuid.uuid4()}"
    try:
        source = work_dir / f"sample{safe_suffix(file.filename)}"
        save_upload(file, source)
        check_audio(source)
        ai = ai_gateway.pipeline()
        T = ai_gateway.types()
        try:
            result = ai.enroll_voice(source, work_dir)
        except T.VoiceQualityRejected as exc:
            raise ApiError(422, "VOICE_QUALITY_REJECTED", exc.message,
                           {"reasons": list(exc.reasons), "speech_seconds": round(exc.speech_seconds, 1),
                            "required_seconds": exc.required_seconds})
        except T.AIError as exc:
            if exc.code == "MODEL_UNAVAILABLE":
                raise ApiError(503, "MODEL_UNAVAILABLE", exc.message, {"model": exc.model or "embedding"})
            if exc.code.startswith("AUDIO_"):
                raise ApiError(422, exc.code, exc.message)
            log.warning("voice enrollment failed: code=%s", exc.code)
            raise ApiError(503 if exc.code in ("EMBEDDING_FAILED", "FFMPEG_FAILED") else 500,
                           exc.code, exc.message)
    except ai_gateway.AIUnavailable as exc:
        raise ApiError(503, "MODEL_UNAVAILABLE", exc.message, {"model": "embedding"})
    finally:
        remove_tree(work_dir)  # the raw sample is never kept

    now = utcnow()
    profile = db.get(VoiceProfile, emp.id)
    if profile is None:
        profile = VoiceProfile(employee_id=emp.id)
        db.add(profile)
    profile.vector = [float(x) for x in result.vector]
    profile.model_id = result.model.model_id
    profile.model_revision = result.model.revision
    profile.dimension = int(result.model.dimension)
    profile.quality_status = result.quality_status
    profile.quality_reasons = list(result.reasons)
    profile.speech_seconds = float(result.speech_seconds)
    profile.consent_at = now
    profile.created_at = now
    db.commit()
    return VoiceEnrollmentOut(status=result.quality_status, quality_status=result.quality_status,
                              speech_seconds=round(float(result.speech_seconds), 1),
                              reasons=list(result.reasons), created_at=now)


@router.delete("/{employee_id}/voice", status_code=204, response_class=Response)
def delete_voice(employee_id: str, current: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    emp = _get(db, employee_id)
    _check_voice_access(emp, current)
    profile = db.get(VoiceProfile, emp.id)
    if profile is None:
        raise not_found("Голосовой профиль")
    db.delete(profile)
    db.commit()
    return Response(status_code=204)
