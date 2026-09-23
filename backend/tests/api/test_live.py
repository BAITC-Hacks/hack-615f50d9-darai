"""Live recording backend tests. Preview uses an explicit TEST-ONLY adapter;
finalization runs through the existing recording path (TestAIAdapter)."""

import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from app import ai_types, live_service
from app.config import get_settings
from app.db import db_session

from .conftest import create_meeting, upload


@dataclass(frozen=True)
class LivePreviewUtterance:
    id: str
    start: float
    end: float
    speaker_label: str | None
    text: str
    is_final: bool


@dataclass(frozen=True)
class LivePreviewResult:
    processed_until_seconds: float
    utterances: list
    preview_status: str = "ready"
    preview_error: dict | None = None
    decoded_seconds: float = 0.0
    has_pending_audio: bool = False
    retry_after_ms: int | None = None


@dataclass(frozen=True)
class LivePreviewRequest:
    container_path: Path
    mime_type: str
    stable_bytes: int
    work_dir: Path
    previous: object
    is_cancelled: Callable
    asr_language: str | None = None
    asr_profile: str | None = None
    on_update: Callable | None = None


def _utts(n: int) -> list:
    return [LivePreviewUtterance(f"preview-{i}", float(i), float(i + 1), None, f"фраза {i}", i < n - 1)
            for i in range(n)]


class FakeLiveAI:
    """TEST-ONLY stand-in for app.live_ai; never used at runtime.

    Default: every call recognizes one more second. `script` (list of callables
    req, n -> result) overrides calls one by one."""

    types = SimpleNamespace(LivePreviewUtterance=LivePreviewUtterance, LivePreviewResult=LivePreviewResult,
                            LivePreviewRequest=LivePreviewRequest)

    def __init__(self):
        self.requests = []
        self.times = []
        self.fail = None
        self.script: list = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def transcribe_preview(self, req):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            data = Path(req.container_path).read_bytes()
            self.requests.append((req, data))
            self.times.append(time.monotonic())
            if self.fail:
                raise self.fail
            n = len(self.requests)
            if self.script:
                return self.script.pop(0)(req, n)
            return LivePreviewResult(processed_until_seconds=float(n), utterances=_utts(n), decoded_seconds=float(n))
        finally:
            with self.lock:
                self.active -= 1


def _wait(cond, timeout: float = 5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("condition not reached")


@pytest.fixture
def live_ai(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "live_preview_check_interval_ms", 50)
    monkeypatch.setattr(settings, "live_preview_min_new_bytes", 1)
    monkeypatch.setattr(settings, "live_preview_error_backoff_ms", 100)
    fake = FakeLiveAI()
    live_service.set_live_test_adapter(fake)
    yield fake
    for sid in list(live_service._workers):
        w = live_service._workers.get(sid)
        live_service.stop_preview(sid)
        if w is not None:
            w.thread.join(5)
    live_service.set_live_test_adapter(None)


@pytest.fixture(scope="module")
def webm() -> bytes:
    out = Path(tempfile.mkdtemp()) / "a.webm"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                    "-c:a", "libopus", "-b:a", "32k", str(out)], check=True)
    return out.read_bytes()


def _chunks(data: bytes, n: int = 4) -> list[bytes]:
    size = len(data) // n + 1
    return [data[i:i + size] for i in range(0, len(data), size)]


def _start(api, mid):
    r = api.post(f"/meetings/{mid}/live", json={"source": "microphone", "mime_type": "audio/webm;codecs=opus"})
    assert r.status_code == 201, r.text
    return r.json()


def _put(api, mid, sid, seq, data):
    return api.c.put(f"/meetings/{mid}/live/{sid}/chunks/{seq}", content=data,
                     headers={"X-CSRF-Token": api.csrf, "Content-Type": "audio/webm;codecs=opus"})


def test_full_live_flow(org, ai, live_ai, webm):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    st = _start(sec, m["id"])
    assert st["state"] == "recording" and st["next_sequence"] == 0 and st["max_chunk_bytes"] == 5242880
    sid = st["session_id"]
    assert sec.post(f"/meetings/{m['id']}/live", json={"source": "display", "mime_type": "audio/webm"}
                    ).json()["error"]["code"] == "LIVE_ALREADY_ACTIVE"
    parts = _chunks(webm)
    for i, p in enumerate(parts):
        r = _put(sec, m["id"], sid, i, p)
        assert r.status_code == 200 and r.json() == {"accepted_sequence": i, "next_sequence": i + 1}
    # duplicate with same bytes: ok, no duplication
    assert _put(sec, m["id"], sid, 1, parts[1]).json() == {"accepted_sequence": 1, "next_sequence": len(parts)}
    r = _put(sec, m["id"], sid, 1, b"other bytes")
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHUNK_CONFLICT"
    r = _put(sec, m["id"], sid, len(parts) + 1, b"xx")
    assert r.json()["error"]["code"] == "CHUNK_OUT_OF_ORDER" and r.json()["error"]["details"]["next_sequence"] == len(parts)

    def settled():
        snap = sec.get(f"/meetings/{m['id']}/live/{sid}").json()
        done = live_ai.requests and live_ai.requests[-1][0].stable_bytes == len(webm) and \
            snap["preview_status"] == "ready" and snap["revision"] == len(live_ai.requests)
        return snap if done else None

    snap = _wait(settled)
    assert snap["received_bytes"] == len(webm) and snap["next_sequence"] == len(parts)
    # preview ran on stable prefixes of the SAME container, coalesced, never per chunk
    assert all(webm.startswith(data) for _, data in live_ai.requests)
    assert all(req.stable_bytes == len(data) for req, data in live_ai.requests)
    assert len(live_ai.requests) <= len(parts) and live_ai.max_active == 1
    n = len(live_ai.requests)
    assert snap["received_audio_seconds"] == float(n) and snap["lag_seconds"] == 0.0
    assert snap["utterances"][0] == {"id": "preview-0", "start": 0.0, "end": 1.0, "speaker_label": None,
                                     "text": "фраза 0", "is_final": len(live_ai.requests) > 1}
    assert snap["draft_tasks"] == [] and snap["recording_id"] is None

    r = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": len(parts)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHUNKS_MISSING"
    r = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": len(parts) - 1})
    assert r.status_code == 202 and r.json()["state"] == "finalizing"
    rec_id = r.json()["recording_id"]
    again = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": len(parts) - 1})
    assert again.status_code == 202 and again.json()["recording_id"] == rec_id
    assert [c[0] for c in ai.calls].count("process") == 1
    # full pipeline got the whole container
    req = next(c[1] for c in ai.calls if c[0] == "process")
    assert req.source_path.read_bytes() == webm
    snap = sec.get(f"/meetings/{m['id']}/live/{sid}").json()
    assert snap["state"] == "done" and snap["recording_id"] == rec_id
    d = sec.get(f"/meetings/{m['id']}").json()
    assert d["recording"]["id"] == rec_id and d["recording"]["processing_status"] == "done"
    assert len(d["utterances"]) == 3 and all(t["status"] == "draft" for t in d["tasks"])
    assert _put(sec, m["id"], sid, len(parts), b"late").json()["error"]["code"] == "LIVE_NOT_RECORDING"


def test_access_and_validation(org, live_ai):
    sec, dana, bolat = org["api"]["sec"], org["api"]["dana"], org["api"]["bolat"]
    m = create_meeting(sec, [org["dana"]["id"]])
    assert dana.post(f"/meetings/{m['id']}/live", json={"source": "microphone", "mime_type": "audio/webm"}
                     ).status_code == 403
    assert bolat.post(f"/meetings/{m['id']}/live", json={"source": "microphone", "mime_type": "audio/webm"}
                      ).status_code == 404
    assert sec.post(f"/meetings/{m['id']}/live", json={"source": "camera", "mime_type": "audio/webm"}).status_code == 422
    assert sec.post(f"/meetings/{m['id']}/live", json={"source": "microphone", "mime_type": "text/html"}
                    ).status_code == 422
    sid = _start(sec, m["id"])["session_id"]
    assert dana.get(f"/meetings/{m['id']}/live/{sid}").status_code == 403  # preview only for editors
    assert _put(dana, m["id"], sid, 0, b"x").status_code == 403
    assert sec.c.put(f"/meetings/{m['id']}/live/{sid}/chunks/0", content=b"x").json()["error"]["code"] == "CSRF_FAILED"
    assert _put(sec, m["id"], sid, 0, b"").status_code == 422
    assert _put(sec, m["id"], sid, 0, b"x" * (5 * 1024 * 1024 + 1)).status_code == 413
    assert sec.get(f"/meetings/{m['id']}/live/{uuid.uuid4()}").status_code == 404
    # empty recording rejected explicitly
    r = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": -1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "LIVE_EMPTY"


def test_cancel_keeps_previous_protocol(org, ai, live_ai, webm):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    assert upload(sec, m["id"]).status_code == 202  # existing file upload still works
    before = sec.get(f"/meetings/{m['id']}").json()
    sid = _start(sec, m["id"])["session_id"]
    assert sec.get(f"/meetings/{m['id']}").json()["recording"]["id"] == before["recording"]["id"]  # start keeps draft
    _put(sec, m["id"], sid, 0, webm[:5000])
    assert sec.post(f"/meetings/{m['id']}/live/{sid}/cancel").status_code == 204
    assert sec.post(f"/meetings/{m['id']}/live/{sid}/cancel").status_code == 204
    snap = sec.get(f"/meetings/{m['id']}/live/{sid}").json()
    assert snap["state"] == "cancelled"
    with db_session() as db:
        s = db.get(live_service.LiveSession, uuid.UUID(sid))
        assert not Path(s.local_path).parent.exists()
    after = sec.get(f"/meetings/{m['id']}").json()
    assert after["recording"]["id"] == before["recording"]["id"] and len(after["tasks"]) == len(before["tasks"])
    assert _put(sec, m["id"], sid, 1, b"x").json()["error"]["code"] == "LIVE_NOT_RECORDING"
    assert sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": 0}
                    ).json()["error"]["code"] == "LIVE_NOT_RECORDING"
    _start(sec, m["id"])  # a new session is allowed after cancel


def test_preview_failure_does_not_break_recording(org, ai, live_ai, webm):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    live_ai.fail = ai_types.AIError("MODEL_UNAVAILABLE", "Не найдены веса", model="asr")
    sid = _start(sec, m["id"])["session_id"]
    parts = _chunks(webm, 2)
    for i, p in enumerate(parts):
        assert _put(sec, m["id"], sid, i, p).status_code == 200
    snap = _wait(lambda: (x := sec.get(f"/meetings/{m['id']}/live/{sid}").json())["preview_status"] == "unavailable"
                 and x)
    assert snap["state"] == "recording" and snap["preview_status"] == "unavailable"
    assert snap["preview_error"]["code"] == "MODEL_UNAVAILABLE" and snap["error"] is None
    r = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": len(parts) - 1})
    assert r.status_code == 202
    assert sec.get(f"/meetings/{m['id']}/live/{sid}").json()["state"] == "done"


def test_restart_and_client_timeout_keep_data(org, ai, live_ai, webm):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    sid = _start(sec, m["id"])["session_id"]
    _put(sec, m["id"], sid, 0, webm)
    with db_session() as db:
        assert live_service.recover_interrupted(db) == 1
    snap = sec.get(f"/meetings/{m['id']}/live/{sid}").json()
    assert snap["state"] == "error" and snap["error"]["code"] == "INTERRUPTED" and snap["received_bytes"] == len(webm)
    r = sec.post(f"/meetings/{m['id']}/live/{sid}/finish", json={"last_sequence": 0})
    assert r.status_code == 202  # accepted data is not lost

    m2 = create_meeting(sec, [org["dana"]["id"]])
    sid2 = _start(sec, m2["id"])["session_id"]
    _put(sec, m2["id"], sid2, 0, webm[:3000])
    # a publishing preview holds the row lock briefly; expire_idle skips locked rows until the next tick
    _wait(lambda: sec.get(f"/meetings/{m2['id']}/live/{sid2}").json()["preview_status"] == "ready")
    with db_session() as db:
        assert live_service.expire_idle(db, datetime.now(timezone.utc) + timedelta(seconds=10)) == 0
        assert live_service.expire_idle(db, datetime.now(timezone.utc) + timedelta(seconds=500)) == 1
    snap = sec.get(f"/meetings/{m2['id']}/live/{sid2}").json()
    assert snap["state"] == "error" and snap["error"]["code"] == "CLIENT_TIMEOUT"
    assert _put(sec, m2["id"], sid2, 1, b"x").json()["error"]["code"] == "LIVE_NOT_RECORDING"


def test_confirmed_meeting_and_meeting_url(org, ai, live_ai):
    sec, dana = org["api"]["sec"], org["api"]["dana"]
    m = create_meeting(sec, [org["dana"]["id"]], meeting_url="https://meet.example.local/abc")
    assert m["meeting_url"] == "https://meet.example.local/abc"
    assert sec.post("/meetings", json={"title": "x", "starts_at": "2026-09-23T10:00:00", "timezone": "Asia/Almaty",
                                       "meeting_url": "javascript:alert(1)"}).status_code == 422
    r = sec.patch(f"/meetings/{m['id']}/link", json={"meeting_url": "http://zoom.local/j/1"})
    assert r.status_code == 200 and r.json() == {"meeting_url": "http://zoom.local/j/1"}
    assert sec.patch(f"/meetings/{m['id']}/link", json={"meeting_url": "ftp://x"}).status_code == 422
    assert dana.patch(f"/meetings/{m['id']}/link", json={"meeting_url": None}).status_code == 403
    assert sec.patch(f"/meetings/{m['id']}/link", json={"meeting_url": None}).json() == {"meeting_url": None}
    # confirmed meeting: no live recording
    upload(sec, m["id"])
    d = sec.get(f"/meetings/{m['id']}").json()
    sec.post(f"/meetings/{m['id']}/confirm", json={"draft_revision": d["draft_revision"], "acknowledge_incomplete": True})
    r = sec.post(f"/meetings/{m['id']}/live", json={"source": "microphone", "mime_type": "audio/webm"})
    assert r.json()["error"]["code"] == "MEETING_CONFIRMED"
