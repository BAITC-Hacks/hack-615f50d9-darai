from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..access import parse_uuid
from ..auth import (
    MIN_PASSWORD_LENGTH,
    CurrentUser,
    authenticate,
    create_session,
    destroy_session,
    generate_temporary_password,
    get_current_user,
    hash_password,
    require_admin,
    revoke_user_sessions,
    verify_password,
)
from ..db import get_db
from ..errors import ApiError, conflict, not_found
from ..models import Employee, User, utcnow
from ..schemas import (
    ChangePasswordIn,
    EmployeeBrief,
    LoginIn,
    LoginOut,
    MeOut,
    Page,
    UserAdminOut,
    UserCreate,
    UserOut,
    UserPatch,
    UserWithTemporaryPassword,
)

router = APIRouter(tags=["auth"])


def user_out(user: User) -> UserOut:
    emp = user.employee
    return UserOut(
        id=user.id, login=user.login, role=user.role, must_change_password=user.must_change_password,
        employee=EmployeeBrief(id=emp.id, fio=emp.fio, position=emp.position, department=emp.department)
        if emp else None,
    )


def user_admin_out(user: User) -> UserAdminOut:
    return UserAdminOut(id=user.id, login=user.login, role=user.role, active=user.active,
                        must_change_password=user.must_change_password, employee_id=user.employee_id,
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


@router.post("/auth/change-password", response_model=LoginOut)
def change_password(body: ChangePasswordIn, response: Response, current: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Own password only. Revokes every session of the user (this one included)
    and issues a fresh session + CSRF token in this response."""
    user = db.get(User, current.id)
    assert user is not None
    if not verify_password(body.current_password, user.password_hash):
        raise ApiError(422, "INVALID_CURRENT_PASSWORD", "Неверный текущий пароль")
    new = body.new_password
    if len(new) < MIN_PASSWORD_LENGTH or new == body.current_password or new.lower() == user.login.lower():
        raise ApiError(422, "WEAK_PASSWORD",
                       f"Новый пароль: не короче {MIN_PASSWORD_LENGTH} символов, не равен текущему и логину")
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    revoke_user_sessions(db, user.id)
    db.commit()
    csrf = create_session(db, user, response)
    return LoginOut(user=user_out(user), csrf_token=csrf)


# ------------------------------------------------------------------ users (admin)

users = APIRouter(prefix="/users", tags=["users"])


@users.get("", response_model=Page[UserAdminOut])
def list_users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
               _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    total = db.scalar(select(func.count()).select_from(User)) or 0
    rows = db.scalars(select(User).order_by(User.login).limit(limit).offset(offset)).all()
    return Page[UserAdminOut](items=[user_admin_out(u) for u in rows], total=total, limit=limit, offset=offset)


def _issue_temporary(db: Session, user: User) -> str:
    temporary = generate_temporary_password()
    user.password_hash = hash_password(temporary)
    user.must_change_password = True
    user.password_changed_at = utcnow()
    return temporary


@users.post("", response_model=UserWithTemporaryPassword, status_code=201)
def create_user(body: UserCreate, _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    emp = db.scalar(select(Employee).where(Employee.id == body.employee_id).with_for_update())
    if emp is None:
        raise not_found("Сотрудник")
    if not emp.active:
        raise conflict("EMPLOYEE_ARCHIVED", "Сотрудник удалён (архивирован)")
    if db.scalar(select(User.id).where(User.login == body.login)) is not None:
        raise conflict("LOGIN_TAKEN", "Логин уже занят")
    if db.scalar(select(User.id).where(User.employee_id == body.employee_id)) is not None:
        raise conflict("EMPLOYEE_HAS_ACCOUNT", "У сотрудника уже есть учётная запись")
    user = User(employee_id=body.employee_id, login=body.login, role=body.role, password_hash="")
    temporary = _issue_temporary(db, user)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise conflict("LOGIN_TAKEN", "Логин или сотрудник уже заняты")
    db.refresh(user)
    return UserWithTemporaryPassword(**user_admin_out(user).model_dump(), temporary_password=temporary)


@users.post("/{user_id}/reset-password", response_model=UserWithTemporaryPassword)
def reset_password(user_id: str, _: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    """Issue a new temporary password; the old one is never readable."""
    user = db.get(User, parse_uuid(user_id, "Пользователь"))
    if user is None:
        raise not_found("Пользователь")
    temporary = _issue_temporary(db, user)
    revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return UserWithTemporaryPassword(**user_admin_out(user).model_dump(), temporary_password=temporary)


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
    if body.active and user.employee_id is not None:
        emp = db.scalar(select(Employee).where(Employee.id == user.employee_id).with_for_update())
        if emp is not None and not emp.active:
            raise conflict("EMPLOYEE_ARCHIVED", "Сотрудник удалён (архивирован): доступ не восстанавливается")
    if body.active is not None:
        user.active = body.active
        if not body.active:
            revoke_user_sessions(db, user.id)
    db.commit()
    db.refresh(user)
    return user_admin_out(user)
