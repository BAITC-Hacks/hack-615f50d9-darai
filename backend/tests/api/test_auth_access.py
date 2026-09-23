from fastapi.testclient import TestClient

from app.main import app

from .conftest import Api, create_meeting, make_employee


def test_login_me_logout_and_csrf(admin):
    c = TestClient(app)
    assert c.get("/auth/me").status_code == 401
    bad = c.post("/auth/login", json={"login": "admin", "password": "wrong"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "INVALID_CREDENTIALS"
    r = c.post("/auth/login", json={"login": "admin", "password": "admin-pass-1"})
    assert r.status_code == 200
    assert "darai_session" in r.cookies and r.cookies.get("darai_csrf") == r.json()["csrf_token"]
    me = c.get("/auth/me").json()
    assert me["role"] == "admin" and me["csrf_token"] == r.json()["csrf_token"]
    # mutating request without CSRF header is refused
    no_csrf = c.post("/employees", json={"fio": "X", "position": "Y", "department": "Z"})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "CSRF_FAILED"
    assert c.post("/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]}).status_code == 204
    assert c.get("/auth/me").status_code == 401


def test_validation_error_envelope(admin):
    r = admin.post("/employees", json={"fio": "", "position": "a"})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR" and body["details"]["fields"]


def test_roles_enforced(org):
    dana = org["api"]["dana"]
    assert dana.post("/employees", json={"fio": "A", "position": "B", "department": "C"}).status_code == 403
    assert dana.get("/users").status_code == 403
    assert dana.get("/system/status").status_code == 403
    r = dana.post("/meetings", json={"title": "x", "starts_at": "2026-09-23T10:00:00", "timezone": "Asia/Almaty",
                                     "participant_ids": []})
    assert r.status_code == 403
    # directory is readable, vectors are never part of the payload
    emp = dana.get(f"/employees/{org['bolat']['id']}").json()
    assert "vector" not in str(emp)


def test_duplicate_login_and_account(admin, org):
    r = admin.post("/users", json={"employee_id": org["guest"]["id"], "login": "dana", "password": "password-123",
                                   "role": "employee"})
    assert r.json()["error"]["code"] == "LOGIN_TAKEN"
    r = admin.post("/users", json={"employee_id": org["dana"]["id"], "login": "dana2", "password": "password-123",
                                   "role": "employee"})
    assert r.json()["error"]["code"] == "EMPLOYEE_HAS_ACCOUNT"


def test_deactivated_user_loses_session(admin, org):
    bolat = org["api"]["bolat"]
    assert bolat.get("/auth/me").status_code == 200
    uid = bolat.user["id"]
    assert admin.patch(f"/users/{uid}", json={"active": False}).status_code == 200
    assert bolat.get("/auth/me").status_code == 401


def test_foreign_meeting_hidden_by_id(org):
    sec, dana, bolat = org["api"]["sec"], org["api"]["dana"], org["api"]["bolat"]
    m = create_meeting(sec, [org["dana"]["id"]])
    assert dana.get(f"/meetings/{m['id']}").status_code == 200
    r = bolat.get(f"/meetings/{m['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert bolat.get("/meetings").json()["total"] == 0
    assert bolat.get(f"/meetings/{m['id']}/export?fmt=docx").status_code == 404
    assert bolat.get("/meetings/not-a-uuid").status_code == 404
    # participant cannot edit
    assert dana.patch(f"/meetings/{m['id']}", json={"title": "hack"}).status_code == 403
    # another secretary who is not organizer cannot see it either
    other = make_employee(Api("admin", "admin-pass-1"), "Другой Секретарь", "sec2", "secretary")
    assert other
    assert Api("sec2", "password-123").get(f"/meetings/{m['id']}").status_code == 404


def test_draft_content_hidden_from_participant(org, ai):
    from .conftest import upload

    sec, dana = org["api"]["sec"], org["api"]["dana"]
    m = create_meeting(sec, [org["dana"]["id"], org["bolat"]["id"]])
    assert upload(sec, m["id"]).status_code == 202
    detail = dana.get(f"/meetings/{m['id']}").json()
    assert detail["content_visible"] is False
    assert detail["utterances"] == [] and detail["tasks"] == [] and detail["summary"] is None
    rec_id = detail["recording"]["id"]
    assert dana.get(f"/meetings/{m['id']}/recordings/{rec_id}").status_code == 404
    assert dana.get(f"/meetings/{m['id']}/recordings/{rec_id}/audio").status_code == 404
    assert dana.get(f"/meetings/{m['id']}/export?fmt=docx").status_code == 404
    # draft tasks are not listed for the assignee
    assert dana.get("/tasks?assignee=me").json()["total"] == 0


def test_voice_profile_access(org, ai):
    dana, bolat = org["api"]["dana"], org["api"]["bolat"]
    from .conftest import wav_bytes

    files = {"file": ("v.wav", wav_bytes(1.0), "audio/wav")}
    r = bolat.post(f"/employees/{org['dana']['id']}/voice", files=files, data={"consent": "true"})
    assert r.status_code == 403
    r = dana.post(f"/employees/{org['dana']['id']}/voice", files=files, data={"consent": "false"})
    assert r.status_code == 422
    r = dana.post(f"/employees/{org['dana']['id']}/voice", files=files, data={"consent": "true"})
    assert r.status_code == 201, r.text
    assert r.json()["quality_status"] == "ok"
    assert "vector" not in r.text
    assert dana.get(f"/employees/{org['dana']['id']}").json()["voice_profile"]["status"] == "ok"
    assert bolat.delete(f"/employees/{org['dana']['id']}/voice").status_code == 403
    assert dana.delete(f"/employees/{org['dana']['id']}/voice").status_code == 204
    assert dana.get(f"/employees/{org['dana']['id']}").json()["voice_profile"]["status"] == "none"


def test_voice_quality_rejected_keeps_previous(org, ai):
    from .conftest import wav_bytes

    dana = org["api"]["dana"]
    files = {"file": ("v.wav", wav_bytes(1.0), "audio/wav")}
    assert dana.post(f"/employees/{org['dana']['id']}/voice", files=files, data={"consent": "true"}).status_code == 201
    ai.enroll_reject = True
    r = dana.post(f"/employees/{org['dana']['id']}/voice", files={"file": ("v.wav", wav_bytes(1.0), "audio/wav")},
                  data={"consent": "true"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VOICE_QUALITY_REJECTED"
    assert r.json()["error"]["details"]["reasons"] == ["too_short"]
    assert dana.get(f"/employees/{org['dana']['id']}").json()["voice_profile"]["status"] == "ok"


def test_invalid_audio_rejected(org, ai):
    dana = org["api"]["dana"]
    r = dana.post(f"/employees/{org['dana']['id']}/voice",
                  files={"file": ("v.wav", b"not audio at all", "audio/wav")}, data={"consent": "true"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "AUDIO_INVALID"
