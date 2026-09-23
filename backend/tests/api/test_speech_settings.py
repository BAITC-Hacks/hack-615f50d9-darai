from .conftest import create_meeting, upload
from .test_live import live_ai, webm, _start, _put, _wait


def test_settings_access_persistence_and_pipeline(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    route = f"/meetings/{m['id']}/speech-settings"
    body = {"asr_language": "kk", "asr_profile": "standard"}
    assert org["api"]["dana"].patch(route, json=body).status_code == 403
    assert sec.patch(route, json={**body, "asr_language": "zz"}).status_code == 422
    assert sec.patch(route, json=body).json() == body
    detail = sec.get(f"/meetings/{m['id']}").json()
    assert detail["asr_language"] == "kk"
    assert upload(sec, m["id"]).status_code == 202
    req = next(c[1] for c in ai.calls if c[0] == "process")
    assert req.meeting.asr_language == "kk" and req.meeting.asr_profile == "standard"


def test_live_uses_settings_and_locks_changes(org, live_ai, webm):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [])
    route = f"/meetings/{m['id']}/speech-settings"
    body = {"asr_language": "ru", "asr_profile": "refined"}
    assert sec.patch(route, json=body).status_code == 200
    sid = _start(sec, m["id"])["session_id"]
    assert sec.patch(route, json=body).status_code == 409
    assert _put(sec, m["id"], sid, 0, webm).status_code == 200
    req = _wait(lambda: live_ai.requests)[0][0]  # preview runs in its own worker thread
    assert req.asr_language == "ru" and req.asr_profile == "refined"
