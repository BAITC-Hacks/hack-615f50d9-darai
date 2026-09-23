from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import db_session
from app.main import app
from app.models import User

from .conftest import Api, activate, wav_bytes


def _login(login: str, password: str):
    c = TestClient(app)
    r = c.post("/auth/login", json={"login": login, "password": password})
    return c, r


def _new_account(admin, fio="Жанова Айым", login="a.zhanova"):
    emp = admin.post("/employees", json={"fio": fio, "position": "Аналитик", "department": "Стратегия"}).json()
    r = admin.post("/users", json={"employee_id": emp["id"], "login": login})
    assert r.status_code == 201, r.text
    return emp, r


def test_admin_creates_account_with_temporary_password(admin):
    emp, r = _new_account(admin)
    body = r.json()
    assert body["role"] == "employee" and body["must_change_password"] is True
    temp = body["temporary_password"]
    assert len(temp) >= 16
    assert r.headers["cache-control"] == "no-store"
    # only the hash is stored; the password cannot be read back
    with db_session() as db:
        stored = db.scalar(select(User.password_hash).where(User.login == "a.zhanova"))
    assert temp not in stored and stored.startswith("scrypt$")
    listed = admin.get("/users").json()["items"]
    assert all("temporary_password" not in u and "password" not in str(u).lower().replace("must_change_password", "")
               for u in listed)
    # admin cannot choose a password anymore
    r2 = admin.post("/users", json={"employee_id": emp["id"], "login": "x.y", "password": "chosen-by-admin"})
    assert r2.status_code == 422
    assert admin.patch(f"/users/{body['id']}", json={"password": "chosen-by-admin"}).status_code == 422


def test_temporary_login_blocks_everything_until_password_change(admin):
    emp, r = _new_account(admin)
    temp = r.json()["temporary_password"]
    c, login = _login("a.zhanova", temp)
    assert login.status_code == 200 and login.json()["user"]["must_change_password"] is True
    csrf = login.json()["csrf_token"]
    h = {"X-CSRF-Token": csrf}
    assert c.get("/auth/me").json()["must_change_password"] is True
    # direct API calls bypassing the UI are refused
    for method, url in [("GET", "/employees/me"), ("GET", "/tasks"), ("GET", "/meetings"),
                        ("GET", "/notifications"), ("GET", f"/employees/{emp['id']}")]:
        resp = c.request(method, url)
        assert resp.status_code == 403 and resp.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED", url
    resp = c.post("/employees/me/voice", headers=h, files={"file": ("v.wav", wav_bytes(), "audio/wav")},
                  data={"consent": "true"})
    assert resp.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    # wrong current / weak new password
    bad = c.post("/auth/change-password", headers=h, json={"current_password": "nope", "new_password": "long-enough-1"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_CURRENT_PASSWORD"
    for weak in ("short", temp, "a.zhanova"):
        w = c.post("/auth/change-password", headers=h, json={"current_password": temp, "new_password": weak})
        assert w.json()["error"]["code"] == "WEAK_PASSWORD"
    # change-password needs CSRF like any mutation
    assert c.post("/auth/change-password", json={"current_password": temp, "new_password": "new-secret-42"}
                  ).json()["error"]["code"] == "CSRF_FAILED"
    ok = c.post("/auth/change-password", headers=h, json={"current_password": temp, "new_password": "new-secret-42"})
    assert ok.status_code == 200 and ok.json()["user"]["must_change_password"] is False
    assert ok.json()["csrf_token"] != csrf
    # the same client continues with the fresh session
    assert c.get("/employees/me").status_code == 200
    # temporary password no longer works; new one does
    assert _login("a.zhanova", temp)[1].status_code == 401
    assert _login("a.zhanova", "new-secret-42")[1].json()["user"]["must_change_password"] is False


def test_password_change_revokes_other_sessions(admin):
    _, r = _new_account(admin)
    activate("a.zhanova", r.json()["temporary_password"], "first-secret-1")
    other = Api("a.zhanova", "first-secret-1")
    me = Api("a.zhanova", "first-secret-1")
    resp = me.post("/auth/change-password", json={"current_password": "first-secret-1",
                                                   "new_password": "second-secret-2"})
    assert resp.status_code == 200
    assert other.get("/auth/me").status_code == 401  # other device logged out
    assert me.c.get("/auth/me").status_code == 200   # current client got a new session cookie


def test_admin_reset_password(admin):
    _, r = _new_account(admin)
    uid = r.json()["id"]
    activate("a.zhanova", r.json()["temporary_password"], "first-secret-1")
    session = Api("a.zhanova", "first-secret-1")
    reset = admin.post(f"/users/{uid}/reset-password")
    assert reset.status_code == 200 and reset.headers["cache-control"] == "no-store"
    new_temp = reset.json()["temporary_password"]
    assert reset.json()["must_change_password"] is True
    assert session.get("/auth/me").status_code == 401  # old sessions revoked
    assert _login("a.zhanova", "first-secret-1")[1].status_code == 401
    c, login = _login("a.zhanova", new_temp)
    assert login.json()["user"]["must_change_password"] is True
    assert c.get("/tasks").json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    # non-admins cannot reset or escalate
    activate("a.zhanova", new_temp, "third-secret-3")
    emp_api = Api("a.zhanova", "third-secret-3")
    assert emp_api.post(f"/users/{uid}/reset-password").status_code == 403
    assert emp_api.patch(f"/users/{uid}", json={"role": "admin"}).status_code == 403
    assert emp_api.post("/users", json={"employee_id": emp_api.user["employee"]["id"], "login": "evil"}).status_code == 403


def test_deactivated_account_cannot_use_old_session_or_login(admin):
    _, r = _new_account(admin)
    uid = r.json()["id"]
    activate("a.zhanova", r.json()["temporary_password"], "first-secret-1")
    s = Api("a.zhanova", "first-secret-1")
    assert admin.patch(f"/users/{uid}", json={"active": False}).json()["active"] is False
    assert s.get("/auth/me").status_code == 401
    assert _login("a.zhanova", "first-secret-1")[1].status_code == 401


def test_own_profile_and_own_voice_only(admin, ai):
    emp, r = _new_account(admin)
    other, r2 = _new_account(admin, "Бекова Сая", "s.bekova")
    activate("a.zhanova", r.json()["temporary_password"], "first-secret-1")
    me = Api("a.zhanova", "first-secret-1")
    prof = me.get("/employees/me").json()
    assert prof["id"] == emp["id"] and prof["login"] == "a.zhanova" and prof["fio"] == "Жанова Айым"
    assert prof["voice_profile"]["status"] == "none"
    # no voice does not block normal work
    assert me.get("/tasks?assignee=me").status_code == 200
    assert me.get("/notifications").status_code == 200
    files = lambda: {"file": ("v.wav", wav_bytes(), "audio/wav")}  # noqa: E731
    assert me.post("/employees/me/voice", files=files(), data={"consent": "true"}).status_code == 201
    assert me.get("/employees/me").json()["voice_profile"]["status"] == "ok"
    # replace; a failed re-enrollment keeps the working profile
    assert me.post("/employees/me/voice", files=files(), data={"consent": "true"}).status_code == 201
    ai.enroll_reject = True
    assert me.post("/employees/me/voice", files=files(), data={"consent": "true"}).status_code == 422
    assert me.get("/employees/me").json()["voice_profile"]["status"] == "ok"
    ai.enroll_reject = False
    # someone else's profile: forbidden by id
    assert me.post(f"/employees/{other['id']}/voice", files=files(), data={"consent": "true"}).status_code == 403
    assert me.delete(f"/employees/{other['id']}/voice").status_code == 403
    # delete own
    assert me.delete("/employees/me/voice").status_code == 204
    assert me.get("/employees/me").json()["voice_profile"]["status"] == "none"


def test_existing_users_unaffected_by_migration(admin):
    me = admin.get("/auth/me").json()
    assert me["must_change_password"] is False  # bootstrap/pre-existing accounts are not forced
