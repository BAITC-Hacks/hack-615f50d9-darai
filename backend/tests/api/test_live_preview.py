"""Live preview scheduling: time-based coalescing, per-window publication, backlog,
model-busy retry, single worker per session, no late publication after cancel/finish.
TEST-ONLY AI adapter (FakeLiveAI); no models."""

import threading
import time
import uuid

from app import live_service
from app.db import db_session

from .conftest import create_meeting
from .test_live import LivePreviewResult, _put, _start, _utts, _wait, live_ai, webm  # noqa: F401


def _snap(api, mid, sid):
    return api.get(f"/meetings/{mid}/live/{sid}").json()


def _session(org):
    sec = org["api"]["sec"]
    m = create_meeting(sec, [org["dana"]["id"]])
    return sec, m["id"], _start(sec, m["id"])["session_id"]


def test_first_preview_runs_on_small_total(org, live_ai, webm):
    sec, mid, sid = _session(org)
    assert _put(sec, mid, sid, 0, webm[:3000]).status_code == 200  # far below the old 48 KB threshold
    _wait(lambda: live_ai.requests)
    assert live_ai.requests[0][0].stable_bytes == 3000
    snap = _wait(lambda: (x := _snap(sec, mid, sid))["revision"] == 1 and x)
    assert snap["utterances"][0]["text"] == "фраза 0" and snap["preview_status"] == "ready"
    assert snap["received_audio_seconds"] == 1.0 and snap["lag_seconds"] == 0.0


def test_window_callback_visible_before_call_ends(org, live_ai, webm):
    release = threading.Event()

    def windowed(req, n):
        req.on_update(LivePreviewResult(processed_until_seconds=1.0, utterances=_utts(1), decoded_seconds=5.0))
        assert release.wait(5)
        return LivePreviewResult(processed_until_seconds=2.0, utterances=_utts(2), decoded_seconds=5.0)

    live_ai.script = [windowed]
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:3000])
    snap = _wait(lambda: (x := _snap(sec, mid, sid))["revision"] == 1 and x)
    assert live_ai.active == 1  # the AI call is still running
    assert snap["utterances"][0]["text"] == "фраза 0" and snap["preview_status"] == "processing"
    assert snap["processed_until_seconds"] == 1.0 and snap["received_audio_seconds"] == 5.0
    assert snap["lag_seconds"] == 4.0
    release.set()
    snap = _wait(lambda: (x := _snap(sec, mid, sid))["preview_status"] == "ready" and x)
    assert snap["revision"] == 2 and len(snap["utterances"]) == 2 and snap["lag_seconds"] == 3.0


def test_backlog_is_processed_without_new_chunk(org, live_ai, webm):
    def pending(req, n):
        return LivePreviewResult(processed_until_seconds=float(n), utterances=_utts(n), decoded_seconds=3.0,
                                 has_pending_audio=True)

    live_ai.script = [pending, pending]
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:3000])
    snap = _wait(lambda: len(live_ai.requests) == 3 and (x := _snap(sec, mid, sid))["preview_status"] == "ready"
                 and x)
    reqs = [r for r, _ in live_ai.requests]
    # same stable snapshot, previous state carried over from the preceding window
    assert {r.stable_bytes for r in reqs} == {3000} and len({r.container_path for r in reqs}) == 1
    assert reqs[0].previous is None and reqs[1].previous.processed_until_seconds == 1.0
    assert snap["processed_until_seconds"] == 3.0 and snap["revision"] == 3 and snap["lag_seconds"] == 0.0
    time.sleep(0.3)
    assert len(live_ai.requests) == 3  # backlog done: idle until the next chunk


def test_pending_without_progress_does_not_loop(org, live_ai, webm):
    stuck = lambda req, n: LivePreviewResult(processed_until_seconds=0.0, utterances=[], decoded_seconds=9.0,  # noqa: E731
                                             has_pending_audio=True)
    live_ai.script = [stuck] * 50
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:3000])
    _wait(lambda: live_ai.requests)
    time.sleep(0.4)
    assert len(live_ai.requests) == 1


def test_not_enough_speech_waits_for_next_chunk(org, live_ai, webm):
    waiting = lambda req, n: LivePreviewResult(processed_until_seconds=0.0, utterances=[], decoded_seconds=0.5,  # noqa: E731
                                               preview_status="waiting")
    live_ai.script = [waiting, waiting]
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:1000])
    _wait(lambda: live_ai.requests)
    time.sleep(0.4)  # 8 check intervals: no busy loop on the same bytes
    assert len(live_ai.requests) == 1
    snap = _snap(sec, mid, sid)
    assert snap["preview_status"] == "waiting" and snap["revision"] == 0
    assert snap["received_audio_seconds"] == 0.5 and snap["lag_seconds"] == 0.5
    _put(sec, mid, sid, 1, webm[1000:3000])
    _wait(lambda: len(live_ai.requests) == 2)
    assert live_ai.requests[1][0].stable_bytes == 3000


def test_model_busy_is_retried_later_without_new_chunk(org, live_ai, webm):
    busy = lambda req, n: LivePreviewResult(processed_until_seconds=0.0, utterances=[], preview_status="waiting",  # noqa: E731
                                            retry_after_ms=300)
    live_ai.script = [busy]
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:3000])
    _wait(lambda: len(live_ai.requests) == 2)
    assert live_ai.times[1] - live_ai.times[0] >= 0.25  # delayed, not a busy loop
    assert live_ai.requests[1][0].stable_bytes == 3000
    snap = _wait(lambda: (x := _snap(sec, mid, sid))["preview_status"] == "ready" and x)
    assert snap["utterances"] and len(live_ai.requests) == 2


def test_one_worker_and_no_parallel_calls_per_session(org, live_ai, webm):
    def slow(req, n):
        time.sleep(0.15)
        return LivePreviewResult(processed_until_seconds=float(n), utterances=_utts(n), decoded_seconds=float(n))

    live_ai.script = [slow] * 20
    sec, mid, sid = _session(org)
    step = 500
    parts = [webm[i:i + step] for i in range(0, min(len(webm), 12 * step), step)]
    for i, p in enumerate(parts):
        assert _put(sec, mid, sid, i, p).status_code == 200
    total = sum(map(len, parts))
    _wait(lambda: live_ai.requests[-1][0].stable_bytes == total if live_ai.requests else False)
    assert live_ai.max_active == 1
    assert len(live_ai.requests) < len(parts)  # chunks coalesced, not one call per chunk
    assert len([w for w in live_service._workers.values() if w.sid == uuid.UUID(sid)]) == 1


def test_cancel_prevents_late_publication(org, live_ai, webm):
    entered, release = threading.Event(), threading.Event()

    def blocked(req, n):
        entered.set()
        assert release.wait(5)
        req.on_update(LivePreviewResult(processed_until_seconds=1.0, utterances=_utts(1)))
        return LivePreviewResult(processed_until_seconds=1.0, utterances=_utts(1), decoded_seconds=1.0)

    live_ai.script = [blocked]
    sec, mid, sid = _session(org)
    _put(sec, mid, sid, 0, webm[:3000])
    assert entered.wait(5)
    worker = live_service._workers[uuid.UUID(sid)]
    assert sec.post(f"/meetings/{mid}/live/{sid}/cancel").status_code == 204
    release.set()
    worker.thread.join(5)
    assert not worker.thread.is_alive()
    snap = _snap(sec, mid, sid)
    assert snap["state"] == "cancelled" and snap["revision"] == 0 and snap["utterances"] == []


def test_finish_prevents_late_publication_and_gets_whole_container(org, ai, live_ai, webm):
    entered, release = threading.Event(), threading.Event()

    def blocked(req, n):
        entered.set()
        assert release.wait(5)
        return LivePreviewResult(processed_until_seconds=1.0, utterances=_utts(1), decoded_seconds=1.0)

    live_ai.script = [blocked]
    sec, mid, sid = _session(org)
    half = len(webm) // 2
    _put(sec, mid, sid, 0, webm[:half])
    assert entered.wait(5)
    worker = live_service._workers[uuid.UUID(sid)]
    _put(sec, mid, sid, 1, webm[half:])
    r = sec.post(f"/meetings/{mid}/live/{sid}/finish", json={"last_sequence": 1})
    assert r.status_code == 202
    release.set()
    worker.thread.join(5)
    assert not worker.thread.is_alive() and len(live_ai.requests) == 1
    req = next(c[1] for c in ai.calls if c[0] == "process")
    assert req.source_path.read_bytes() == webm  # preview never truncates what finalization gets
    with db_session() as db:
        s = db.get(live_service.LiveSession, uuid.UUID(sid))
        assert s.revision == 0 and s.preview_utterances == []
