"""Backend API tests against a real PostgreSQL + pgvector database.

TEST_DATABASE_URL must point to a disposable database server; the test
database ``darai_test`` is dropped and recreated per session. The AI
pipeline is replaced by ``TestAIAdapter`` (tests/api/fake_ai.py) — these
tests verify CRM/DB/HTTP behaviour, NOT model quality.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_BASE = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://darai:darai@127.0.0.1:55433/darai")
_DATA = tempfile.mkdtemp(prefix="darai-test-")
_TEST_DB = "darai_test"

os.environ.update({
    "DATABASE_URL": _BASE.rsplit("/", 1)[0] + "/" + _TEST_DB,
    "DATA_DIR": _DATA,
    "REMINDERS_ENABLED": "false",
    "RUN_MIGRATIONS_ON_START": "true",
    "ADMIN_LOGIN": "admin",
    "ADMIN_PASSWORD": "admin-pass-1",
    "LLM_MODEL": "test-model",
    "SESSION_TTL_HOURS": "1",
})


def _recreate_db() -> None:
    import psycopg

    admin_url = _BASE.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {_TEST_DB} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {_TEST_DB}")


_recreate_db()

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app import ai_gateway  # noqa: E402
from app.db import db_session  # noqa: E402
from app.main import app  # noqa: E402

from .fake_ai import TestAIAdapter  # noqa: E402

_TABLES = ["notifications", "tasks", "utterances", "speakers", "recordings", "meeting_participants",
           "meetings", "voice_profiles", "auth_sessions", "users", "employees"]


@pytest.fixture(scope="session")
def client_app():
    with TestClient(app) as c:  # runs lifespan: migrations + bootstrap admin
        yield c


@pytest.fixture
def ai():
    adapter = TestAIAdapter()
    ai_gateway.set_test_adapter(adapter)
    yield adapter
    ai_gateway.set_test_adapter(None)


@pytest.fixture(autouse=True)
def clean_db(client_app):
    with db_session() as db:
        db.execute(text("TRUNCATE " + ", ".join(_TABLES) + " CASCADE"))
        db.commit()
        from app.auth import bootstrap_admin

        bootstrap_admin(db)
    yield


class Api:
    """Thin client that keeps one session cookie jar + CSRF token per user."""

    def __init__(self, login: str, password: str):
        self.c = TestClient(app)
        r = self.c.post("/auth/login", json={"login": login, "password": password})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        self.user = r.json()["user"]

    def _h(self, kw):
        kw.setdefault("headers", {})["X-CSRF-Token"] = self.csrf
        return kw

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, **kw):
        return self.c.post(url, **self._h(kw))

    def patch(self, url, **kw):
        return self.c.patch(url, **self._h(kw))

    def delete(self, url, **kw):
        return self.c.delete(url, **self._h(kw))


@pytest.fixture
def admin(client_app) -> Api:
    return Api("admin", "admin-pass-1")


def make_employee(admin: Api, fio: str, login: str | None = None, role: str = "employee") -> dict:
    r = admin.post("/employees", json={"fio": fio, "position": "Специалист", "department": "Отдел"})
    assert r.status_code == 201, r.text
    emp = r.json()
    if login:
        r = admin.post("/users", json={"employee_id": emp["id"], "login": login, "role": role})
        assert r.status_code == 201, r.text
        activate(login, r.json()["temporary_password"])
    return emp


def activate(login: str, temporary: str, new_password: str = "password-123") -> None:
    """First login with the temporary password + mandatory change."""
    c = TestClient(app)
    r = c.post("/auth/login", json={"login": login, "password": temporary})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True, r.text
    r = c.post("/auth/change-password", json={"current_password": temporary, "new_password": new_password},
               headers={"X-CSRF-Token": r.json()["csrf_token"]})
    assert r.status_code == 200, r.text


@pytest.fixture
def org(admin):
    """secretary + two employees with accounts + one without an account."""
    sec = make_employee(admin, "Секретарёва Айгуль", "sec", "secretary")
    dana = make_employee(admin, "Ахметова Дана", "dana")
    bolat = make_employee(admin, "Серіков Болат", "bolat")
    guest = make_employee(admin, "Гость Без Аккаунта")
    return {"sec": sec, "dana": dana, "bolat": bolat, "guest": guest,
            "api": {"sec": Api("sec", "password-123"), "dana": Api("dana", "password-123"),
                    "bolat": Api("bolat", "password-123")}}


def wav_bytes(seconds: float = 1.0) -> bytes:
    import io

    import numpy as np
    import soundfile as sf

    t = np.linspace(0, seconds, int(16000 * seconds), endpoint=False)
    buf = io.BytesIO()
    sf.write(buf, (0.2 * np.sin(2 * np.pi * 220 * t)).astype("float32"), 16000, format="WAV")
    return buf.getvalue()


def create_meeting(api: Api, participant_ids: list[str], **extra) -> dict:
    body = {"title": "Планёрка", "starts_at": "2026-09-23T10:00:00", "timezone": "Asia/Almaty",
            "agenda": "Бюджет", "participant_ids": participant_ids, **extra}
    r = api.post("/meetings", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def upload(api: Api, meeting_id: str, data: bytes | None = None):
    return api.post(f"/meetings/{meeting_id}/recordings",
                    files={"file": ("meeting.wav", data or wav_bytes(), "audio/wav")})
