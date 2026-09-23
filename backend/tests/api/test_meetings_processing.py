import uuid

from sqlalchemy import select

from app import ai_types
from app.db import db_session
from app.models import Notification, Recording, VoiceProfile, utcnow
from app.processing import recover_interrupted, run_recording_job

from .conftest import create_meeting, upload, wav_bytes


def _notifications(api, event=None):
    items = api.get("/notifications?limit=200").json()["items"]
    return [n for n in items if event is None or n["event_type"] == event]


def test_create_meeting_json_and_invitations(org):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"], org["guest"]["id"]])
    assert m["starts_at"] == "2026-09-23T05:00:00Z"  # 10:00 Asia/Almaty (+05:00)
    assert m["approval_status"] == "draft" and m["recording"] is None and m["draft_revision"] == 0
    assert {p["fio"] for p in m["participants"]} == {"Ахметова Дана", "Гость Без Аккаунта"}
    assert len(_notifications(org["api"]["dana"], "meeting_invitation")) == 1
    assert _notifications(org["api"]["bolat"]) == []
    # adding a participant later invites only the new one, once
    ids = [org["dana"]["id"], org["guest"]["id"], org["bolat"]["id"]]
    assert sec.patch(f"/meetings/{m['id']}", json={"participant_ids": ids}).status_code == 200
    assert sec.patch(f"/meetings/{m['id']}", json={"participant_ids": ids}).status_code == 200
    assert len(_notifications(org["api"]["bolat"], "meeting_invitation")) == 1
    assert len(_notifications(org["api"]["dana"], "meeting_invitation")) == 1


def test_create_meeting_validation(org):
    sec = org["api"]["sec"]
    r = sec.post("/meetings", json={"title": "x", "starts_at": "2026-09-23T10:00:00", "timezone": "Mars/Base",
                                    "participant_ids": []})
    assert r.status_code == 422
    r = sec.post("/meetings", json={"title": "x", "starts_at": "2026-09-23T10:00:00", "timezone": "Asia/Almaty",
                                    "participant_ids": [str(uuid.uuid4())]})
    assert r.status_code == 422


def test_multipart_meeting_with_recording(org, ai):
    sec = org["api"]["sec"]
    r = sec.post("/meetings", data={"title": "Быстрая", "starts_at": "2026-09-23T10:00:00+05:00",
                                    "timezone": "Asia/Almaty", "participant_ids[]": [org["dana"]["id"]]},
                 files={"file": ("m.wav", wav_bytes(), "audio/wav")})
    assert r.status_code == 202, r.text
    m = sec.get(f"/meetings/{r.json()['id']}").json()
    assert m["recording"]["processing_status"] == "done"
    assert len(m["participants"]) == 1


def test_pipeline_result_persisted_unknown_speaker_stays_unknown(org, ai):
    sec = org["api"]["sec"]
    dana_id, sec_emp = org["dana"]["id"], org["sec"]["id"]
    m = create_meeting(sec, [dana_id, org["bolat"]["id"], sec_emp])
    ai.speaker_map = {"SPEAKER_00": uuid.UUID(sec_emp), "SPEAKER_01": uuid.UUID(dana_id), "SPEAKER_02": None}
    r = upload(sec, m["id"])
    assert r.status_code == 202 and r.json()["processing_status"] == "processing"
    assert ai.stages == ["normalizing", "transcribing", "diarizing", "aligning", "identifying", "extracting"]
    d = sec.get(f"/meetings/{m['id']}").json()
    rec = d["recording"]
    assert rec["processing_status"] == "done" and rec["stage"] == "done" and rec["extraction"]["status"] == "ok"
    assert rec["languages"] == ["ru", "kk"] and rec["audio_url"]
    sp = {s["label"]: s for s in d["speakers"]}
    assert sp["SPEAKER_00"]["confirmed_employee_id"] == sec_emp
    assert sp["SPEAKER_02"]["proposed_employee_id"] is None
    assert sp["SPEAKER_02"]["confirmed_employee_id"] is None and sp["SPEAKER_02"]["review_required"] is True
    assert [u["id"] for u in d["utterances"]] == sorted(u["id"] for u in d["utterances"])
    assert d["utterances"][2]["uncertain"] is True
    tasks = {t["task"]: t for t in d["tasks"]}
    report = tasks["Подготовить отчёт"]
    assert report["from"] == sec_emp and report["to"] == dana_id and report["status"] == "draft"
    assert report["source_utterance_ids"] == [d["utterances"][0]["id"]]
    assert report["deadline_at"] == "2026-09-25T18:59:59Z"
    smeta = tasks["Проверить смету"]
    assert smeta["from"] == "SPEAKER_02" and smeta["to"] is None and smeta["needs_review"] is True
    assert d["summary"] == "Обсудили отчёт и смету."
    assert d["draft_revision"] >= 1
    assert len(_notifications(sec, "protocol_ready")) == 1
    # assignee is NOT notified about drafts
    assert _notifications(org["api"]["dana"], "task_assigned") == []
    audio = sec.get(rec["audio_url"])
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"


def test_only_compatible_participant_profiles_are_candidates(org, ai):
    sec = org["api"]["sec"]
    dana_id, bolat_id, guest_id = org["dana"]["id"], org["bolat"]["id"], org["guest"]["id"]
    enroll = {"file": ("v.wav", wav_bytes(), "audio/wav")}
    assert org["api"]["dana"].post(f"/employees/{dana_id}/voice", files=enroll, data={"consent": "true"}).status_code == 201
    assert org["api"]["bolat"].post(f"/employees/{bolat_id}/voice", files={"file": ("v.wav", wav_bytes(), "audio/wav")},
                                    data={"consent": "true"}).status_code == 201
    with db_session() as db:  # profile from another model version
        db.add(VoiceProfile(employee_id=uuid.UUID(guest_id), vector=[1.0, 0.0, 0.0, 0.0], model_id="old/model",
                            model_revision="0", dimension=4, quality_status="ok", quality_reasons=[],
                            speech_seconds=20.0, consent_at=utcnow()))
        db.commit()
    assert sec.get(f"/employees/{guest_id}").json()["voice_profile"]["status"] == "incompatible"
    m = create_meeting(sec, [dana_id, guest_id])  # bolat has a profile but is not invited
    upload(sec, m["id"])
    req = next(c[1] for c in ai.calls if c[0] == "process")
    assert [str(c.employee_id) for c in req.candidates] == [dana_id]
    assert {str(p.employee_id) for p in req.meeting.participants} == {dana_id, guest_id}


def test_ai_error_is_stored_not_faked(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    ai.fail_with = ai_types.AIError("MODEL_UNAVAILABLE", "Не найдены веса faster-whisper", stage="transcribing",
                                    model="asr")
    upload(sec, m["id"])
    d = sec.get(f"/meetings/{m['id']}").json()
    rec = d["recording"]
    assert rec["processing_status"] == "error" and rec["stage"] == "transcribing"
    assert rec["error_code"] == "MODEL_UNAVAILABLE" and d["utterances"] == [] and d["tasks"] == []
    assert len(_notifications(sec, "processing_failed")) == 1
    # confirming an errored protocol is impossible
    r = sec.post(f"/meetings/{m['id']}/confirm", json={"draft_revision": d["draft_revision"],
                                                        "acknowledge_incomplete": True})
    assert r.json()["error"]["code"] == "RECORDING_NOT_READY"
    # retry runs the full pipeline with a new generation
    ai.fail_with = None
    r = sec.post(f"/meetings/{m['id']}/recordings/{rec['id']}/retry")
    assert r.status_code == 202 and r.json()["generation"] == 2
    d = sec.get(f"/meetings/{m['id']}").json()
    assert d["recording"]["processing_status"] == "done" and len(d["utterances"]) == 3
    assert sec.post(f"/meetings/{m['id']}/recordings/{rec['id']}/retry").json()["error"]["code"] == "RETRY_NOT_ALLOWED"


def test_extraction_error_is_distinct_from_no_tasks_and_retry_extracts_only(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    ai.extraction_error = "LLM_INVALID_RESPONSE"
    upload(sec, m["id"])
    d = sec.get(f"/meetings/{m['id']}").json()
    rec = d["recording"]
    assert rec["processing_status"] == "done"
    assert rec["extraction"] == {"status": "error", "error_code": "LLM_INVALID_RESPONSE",
                                 "error_message": "LLM дважды вернула невалидный JSON"}
    assert d["tasks"] == [] and d["summary"] is None and len(d["utterances"]) == 3
    assert len(_notifications(sec, "processing_failed")) == 1
    ai.extraction_error = None
    ai.calls.clear()
    r = sec.post(f"/meetings/{m['id']}/recordings/{rec['id']}/retry")
    assert r.status_code == 202
    assert [c[0] for c in ai.calls] == ["extract"]  # transcript is reused
    lines = ai.calls[0][1]
    assert [l.id for l in lines] == [u["id"] for u in d["utterances"]]
    d2 = sec.get(f"/meetings/{m['id']}").json()
    assert d2["recording"]["extraction"]["status"] == "ok" and len(d2["tasks"]) == 2
    assert [u["id"] for u in d2["utterances"]] == [u["id"] for u in d["utterances"]]
    assert d2["tasks"][0]["source_utterance_ids"][0] in {u["id"] for u in d["utterances"]}


def test_stale_generation_result_discarded(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    ai.fail_with = ai_types.AIError("ASR_FAILED", "boom", stage="transcribing")
    upload(sec, m["id"])
    rec_id = sec.get(f"/meetings/{m['id']}").json()["recording"]["id"]
    ai.fail_with = None

    def bump_generation():  # another retry wins while this job is running
        with db_session() as db:
            rec = db.get(Recording, uuid.UUID(rec_id))
            rec.generation += 1
            db.commit()

    ai.hook = bump_generation
    sec.post(f"/meetings/{m['id']}/recordings/{rec_id}/retry")
    d = sec.get(f"/meetings/{m['id']}").json()
    assert d["utterances"] == []  # generation 2 result was not written
    assert d["recording"]["generation"] == 3 and d["recording"]["processing_status"] == "processing"


def test_upload_conflicts(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    assert upload(sec, m["id"], b"garbage").json()["error"]["code"] == "AUDIO_INVALID"
    # a processing recording blocks new uploads
    upload(sec, m["id"])
    with db_session() as db:
        rec = db.scalar(select(Recording).where(Recording.meeting_id == uuid.UUID(m["id"])))
        rec.processing_status = "processing"
        db.commit()
    assert upload(sec, m["id"]).json()["error"]["code"] == "RECORDING_PROCESSING"


def test_restart_marks_interrupted_jobs(org, ai):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    upload(sec, m["id"])
    with db_session() as db:
        rec = db.scalar(select(Recording).where(Recording.meeting_id == uuid.UUID(m["id"])))
        rec.processing_status, rec.stage = "processing", "diarizing"
        db.commit()
        assert recover_interrupted(db) == 1
        rec_id, gen = rec.id, rec.generation
    d = sec.get(f"/meetings/{m['id']}").json()["recording"]
    assert d["processing_status"] == "error" and d["error_code"] == "INTERRUPTED" and d["stage"] == "diarizing"
    # a late job from before the restart cannot overwrite the error
    ai.calls.clear()
    run_recording_job(rec_id, gen, "full")
    assert ai.calls == []
    with db_session() as db:
        n = db.scalars(select(Notification).where(Notification.event_type == "processing_failed")).all()
        assert len(n) == 1  # only the secretary (organizer == secretary)
