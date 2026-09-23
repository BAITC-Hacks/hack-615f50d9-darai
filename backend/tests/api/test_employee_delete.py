import uuid

from app.db import db_session
from app.models import User

from .conftest import Api, create_meeting, make_employee, upload, wav_bytes


def _ids(api):
    return {e["id"] for e in api.get("/employees?limit=200").json()["items"]}


def test_secretary_archives_employee_with_account(org, ai):
    sec, dana, bolat = org["api"]["sec"], org["api"]["dana"], org["api"]["bolat"]
    did = org["dana"]["id"]
    # history: confirmed protocol with a task for Dana
    m = create_meeting(sec, [did, org["sec"]["id"]])
    ai.speaker_map = {"SPEAKER_00": uuid.UUID(org["sec"]["id"]), "SPEAKER_01": uuid.UUID(did)}
    upload(sec, m["id"])
    d = sec.get(f"/meetings/{m['id']}").json()
    assert sec.post(f"/meetings/{m['id']}/confirm",
                    json={"draft_revision": d["draft_revision"], "acknowledge_incomplete": True}).status_code == 200
    assert dana.post("/employees/me/voice", files={"file": ("v.wav", wav_bytes(), "audio/wav")},
                     data={"consent": "true"}).status_code == 201

    listed = {e["id"]: e for e in sec.get("/employees").json()["items"]}
    assert listed[did]["can_delete"] is True
    assert listed[org["sec"]["id"]]["can_delete"] is False  # own card
    total_before = sec.get("/employees").json()["total"]

    r = sec.delete(f"/employees/{did}")
    assert r.status_code == 204 and r.content == b""
    assert sec.delete(f"/employees/{did}").status_code == 204  # idempotent

    # sessions revoked, login impossible, access not restorable
    assert dana.get("/auth/me").status_code == 401
    from fastapi.testclient import TestClient
    from app.main import app
    assert TestClient(app).post("/auth/login", json={"login": "dana", "password": "password-123"}).status_code == 401
    admin = Api("admin", "admin-pass-1")
    uid = next(u["id"] for u in admin.get("/users").json()["items"] if u["login"] == "dana")
    assert admin.patch(f"/users/{uid}", json={"active": True}).json()["error"]["code"] == "EMPLOYEE_ARCHIVED"
    assert admin.post("/users", json={"employee_id": did, "login": "dana.new"}).json()["error"]["code"] == \
        "EMPLOYEE_ARCHIVED"
    assert admin.post(f"/employees/{did}/voice", files={"file": ("v.wav", wav_bytes(), "audio/wav")},
                      data={"consent": "true"}).json()["error"]["code"] == "EMPLOYEE_ARCHIVED"

    # gone from directory and new meetings, total follows the filter
    assert did not in _ids(sec)
    assert sec.get("/employees").json()["total"] == total_before - 1
    assert did in {e["id"] for e in sec.get("/employees?active=all&limit=200").json()["items"]}
    r = sec.post("/meetings", json={"title": "x", "starts_at": "2026-09-24T10:00:00", "timezone": "Asia/Almaty",
                                    "participant_ids": [did]})
    assert r.status_code == 422
    new = create_meeting(sec, [org["bolat"]["id"]])
    assert sec.patch(f"/meetings/{new['id']}", json={"participant_ids": [org["bolat"]["id"], did]}).status_code == 422

    # history intact: participant, speaker binding, confirmed task still point to Dana
    h = sec.get(f"/meetings/{m['id']}").json()
    assert did in {p["employee_id"] for p in h["participants"]}
    assert any(s["confirmed_employee_id"] == did for s in h["speakers"])
    task = next(t for t in h["tasks"] if t["to"] == did)
    assert task["status"] == "confirmed" and task["execution_status"] == "in_progress" and task["to_fio"] == "Ахметова Дана"
    card = sec.get(f"/employees/{did}").json()
    assert card["active"] is False and card["can_delete"] is False and card["voice_profile"]["status"] == "ok"



def test_archived_voice_not_a_candidate(org, ai):
    sec, dana = org["api"]["sec"], org["api"]["dana"]
    did, bid = org["dana"]["id"], org["bolat"]["id"]
    for api in (dana, org["api"]["bolat"]):
        assert api.post("/employees/me/voice", files={"file": ("v.wav", wav_bytes(), "audio/wav")},
                        data={"consent": "true"}).status_code == 201
    m = create_meeting(sec, [did, bid])  # Dana invited before archiving
    assert sec.delete(f"/employees/{did}").status_code == 204
    upload(sec, m["id"])
    req = next(c[1] for c in ai.calls if c[0] == "process")
    assert [str(c.employee_id) for c in req.candidates] == [bid]
    # still listed as participant of that meeting (history), editing others keeps her
    assert sec.patch(f"/meetings/{m['id']}", json={"title": "renamed"}).status_code == 200


def test_employee_without_account_and_permissions(org, admin):
    sec, dana = org["api"]["sec"], org["api"]["dana"]
    gid = org["guest"]["id"]
    assert dana.delete(f"/employees/{gid}").status_code == 403  # role employee
    assert dana.get(f"/employees/{gid}").json()["can_delete"] is False
    assert sec.delete(f"/employees/{uuid.uuid4()}").status_code == 404
    assert sec.delete(f"/employees/{org['sec']['id']}").json()["error"]["code"] == "SELF_DELETE_FORBIDDEN"
    # secretary cannot delete a secretary or an admin
    other_sec = make_employee(admin, "Второй Секретарь", "sec2", "secretary")
    r = sec.delete(f"/employees/{other_sec['id']}")
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN"
    admin_emp = admin.user["employee"]["id"]
    assert sec.delete(f"/employees/{admin_emp}").status_code == 403
    assert sec.get(f"/employees/{admin_emp}").json()["can_delete"] is False
    # no account -> secretary may archive
    assert sec.get(f"/employees/{gid}").json()["can_delete"] is True
    assert sec.delete(f"/employees/{gid}").status_code == 204
    assert gid not in _ids(sec)
    # admin can delete a secretary
    assert admin.get(f"/employees/{other_sec['id']}").json()["can_delete"] is True
    assert admin.delete(f"/employees/{other_sec['id']}").status_code == 204
    # CSRF still required
    assert sec.c.delete(f"/employees/{org['bolat']['id']}").json()["error"]["code"] == "CSRF_FAILED"


def test_last_admin_rule_and_admin_self(admin):
    from types import SimpleNamespace

    from app.serializers import delete_denial

    me = admin.user["employee"]["id"]
    assert admin.delete(f"/employees/{me}").json()["error"]["code"] == "SELF_DELETE_FORBIDDEN"
    second = make_employee(admin, "Второй Админ", "admin2", "admin")
    assert admin.get(f"/employees/{second['id']}").json()["can_delete"] is True
    assert admin.delete(f"/employees/{second['id']}").status_code == 204
    # LAST_ADMIN: via HTTP the caller is itself an active admin, so the rule is checked directly
    target = SimpleNamespace(id=uuid.uuid4(), user=SimpleNamespace(role="admin", active=True))
    caller = SimpleNamespace(role="admin", employee_id=uuid.uuid4())
    assert delete_denial(target, caller, 1)[1] == "LAST_ADMIN"
    assert delete_denial(target, caller, 2) is None
