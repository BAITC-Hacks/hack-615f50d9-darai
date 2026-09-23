from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..access import parse_uuid
from ..auth import (
    CurrentUser,
    authenticate,
    create_session,
    destroy_session,
    get_current_user,
    hash_password,
    require_admin,
    revoke_user_sessions,
)
from ..db import get_db
from ..errors import ApiError, conflict, not_found
from ..models import Employee, User
from ..schemas import (
    EmployeeBrief,
    LoginIn,
    LoginOut,
    MeOut,
    Page,
    UserAdminOut,
    UserCreate,
    UserOut,
    UserPatch,
)

router = APIRouter(tags=["auth"])


def user_out(user: User) -> UserOut:
    emp = user.employee
    return UserOut(
        id=user.id, login=user.login, role=user.role,
        employee=EmployeeBrief(id=emp.id, fio=emp.fio, position=emp.position, department=emp.department)
        if emp else None,
    )


def user_admin_out(user: User) -> UserAdminOut:
    return UserAdminOut(id=user.id, login=user.login, role=user.role, active=user.active,
                        employee_id=user.employee_id,
                        employee_fio=user.employee.fio if user.employee else None, created_at=user.created_at)


@router.post("/auth/login", response_model=LoginOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = authenticate(db, body.login, body.password)
    csrf = create_session(db, user, response)
    return LoginOut(user=user_out(user), csrf_token=csrf)


@router.post("/auth/logout", status_code=204, response_class=Response)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    destroy_session(db, request, response)
    response.status_code = 204
    return response


@router.get("/auth/me", response_model=MeOut)
def me(current: CurrentUser = Depends(get_current_user)):
    return MeOut(**user_out(current.user).model_dump(), csrf_token=current.session.csrf_token)


# ------------------------------------------------------------------ users (admin)

users = APIRouter(prefix="/users", tags=["users"])


@users.get("", response_model=Page[UserAdminOut])
def list_users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(User)) or 0
    rows = db.scalars(select(User).order_by(User.login).limit(limit).offset(offset)).all()
    return Page[UserAdminOut](items=[user_admin_out(u) for u in rows], total=total, limit=limit, offset=offset)


@users.post("", response_model=UserAdminOut, status_code=201)
def create_user(body: UserCreate, _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    if db.get(Employee, body.employee_id) is None:
        raise not_found("Сотрудник")
    if db.scalar(select(User.id).where(User.login == body.login)) is not None:
        raise conflict("LOGIN_TAKEN", "Логин уже занят")
    if db.scalar(select(User.id).where(User.employee_id == body.employee_id)) is not None:
        raise conflict("EMPLOYEE_HAS_ACCOUNT", "У сотрудника уже есть учётная запись")
    user = User(employee_id=body.employee_id, login=body.login, password_hash=hash_password(body.password),
                role=body.role)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise conflict("LOGIN_TAKEN", "Логин или сотрудник уже заняты")
    db.refresh(user)
    return user_admin_out(user)


@users.patch("/{user_id}", response_model=UserAdminOut)
def patch_user(user_id: str, body: UserPatch, current: CurrentUser = Depends(require_admin),
               db: Session = Depends(get_db)):
    user = db.get(User, parse_uuid(user_id, "Пользователь"))
    if user is None:
        raise not_found("Пользователь")
    if user.id == current.id and (body.active is False or (body.role and body.role != "admin")):
        raise ApiError(409, "CONFLICT", "Нельзя снять права администратора или деактивировать себя")
    if body.role is not None:
        user.role = body.role
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        revoke_user_sessions(db, user.id)
    if body.active is not None:
        user.active = body.active
        if not body.active:
            revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return user_admin_out(user)
