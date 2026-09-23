"""Live preview: pure merge logic + transcribe_preview behaviour with a FAKE ASR model.

Real-model timing/quality: test_live_real.py and scripts/ai/live_simulate.py.
"""

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.ai_types import AIError
from app.live_ai import (
    LiveConfig,
    find_holes,
    _words_of,
    merge_window,
    new_words_after,
    quiet_point,
    snapshot,
    transcribe_preview,
)
from app.live_ai_types import LivePreviewRequest, LivePreviewResult, LivePreviewState, PreviewWord
from app.ml import ModelUnavailable, compute_lock

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ai"


def W(s, e, t):
    return PreviewWord(s, e, " " + t)


def cfg(**kw):
    c = LiveConfig()
    c.window, c.overlap, c.guard, c.utterance_gap, c.max_utterance = 12.0, 1.5, 1.5, 1.0, 20.0
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def texts(state):
    return [u.text for u in snapshot(state)]


def test_overlap_is_not_duplicated_and_nothing_lost_at_boundary():
    c = cfg()
    # window 1: 0-12 s, live edge -> words ending after 10.5 stay tentative
    w1 = [W(0.5, 1.0, "Раз"), W(1.1, 1.6, "два."), W(3.0, 3.5, "Три"), W(9.8, 10.4, "четыре"), W(10.6, 11.2, "пять")]
    s1 = merge_window(LivePreviewState(), w1, 12.0, True, c)
    assert s1.committed_until == pytest.approx(10.4)
    assert [w.text.strip() for w in s1.tentative] == ["пять"]
    # window 2 starts 1.5 s earlier; re-recognizes "четыре" with shifted times, "пять" now committed
    w2 = [W(9.75, 10.45, "четыре"), W(10.55, 11.2, "пять"), W(11.3, 11.9, "шесть."), W(14.0, 14.5, "Семь")]
    s2 = merge_window(s1, w2, 22.0, True, c)
    words = " ".join(texts(s2))
    assert words.count("четыре") == 1 and words.count("пять") == 1
    assert "шесть." in words and "Семь" in words


def test_unrecognized_voiced_audio_is_not_skipped():
    """Model returned no words for voiced audio at the window end: keep it for the next window."""
    c = cfg()
    s1 = merge_window(LivePreviewState(), [W(0.5, 1.0, "Раз.")], 12.0, True, c, voiced=[(0.4, 1.1), (4.0, 7.0)])
    assert s1.committed_until == pytest.approx(4.0)
    s2 = merge_window(LivePreviewState(), [W(0.5, 1.0, "Раз.")], 12.0, True, c, voiced=[(0.4, 1.1)])
    assert s2.committed_until == pytest.approx(9.0)   # real silence may be skipped


def test_same_word_later_in_time_is_kept():
    c = cfg()
    s1 = merge_window(LivePreviewState(), [W(0.5, 1.0, "да"), W(1.2, 1.6, "да.")], 12.0, True, c)
    assert s1.committed_until == pytest.approx(9.0)   # pause after the words is committed too
    # next window re-covers the edge from 7.5 s: the same words spoken again at 13 s are new speech
    s2 = merge_window(s1, [W(13.0, 13.4, "да"), W(13.5, 13.9, "да.")], 20.0, True, c)
    assert texts(s2) == ["да да.", "да да."]
    assert [u.start for u in snapshot(s2)] == [0.5, 13.0]


def test_first_word_with_stretched_start_is_not_dropped():
    """Whisper often starts the first word after a pause too early (midpoint before the committed point)."""
    c = cfg()
    s1 = merge_window(LivePreviewState(), [W(0.5, 17.8, "отчёт.")], 19.5, True, c)
    assert s1.committed_until == pytest.approx(17.8)
    s2 = merge_window(s1, [W(17.0, 18.3, "Также"), W(18.4, 19.0, "нужно")], 30.0, True, c)
    assert "Также нужно" in " ".join(texts(s2))


def test_silence_creates_nothing_and_advances():
    c = cfg()
    s = merge_window(LivePreviewState(), [], 12.0, True, c)
    assert snapshot(s) == [] and s.committed_until == pytest.approx(12.0 - 1.5 - 1.5)


def test_utterances_close_on_sentence_or_pause_and_ids_are_stable():
    c = cfg()
    s1 = merge_window(LivePreviewState(), [W(0.1, 0.8, "Привет"), W(0.9, 1.5, "всем."), W(1.9, 2.4, "Начнём"),
                                           W(9.0, 9.5, "итак")], 12.0, True, c)
    snap1 = snapshot(s1)
    assert [(u.id, u.is_final) for u in snap1] == [("preview-0", True), ("preview-1", True), ("preview-2", False)]
    s2 = merge_window(s1, [W(9.0, 9.5, "итак"), W(9.6, 10.2, "бюджет."), W(15, 15.5, "Дальше")], 20.0, True, c)
    snap2 = snapshot(s2)
    assert snap2[:2] == snap1[:2]                       # final utterances never change
    assert snap2[2].id == "preview-2" and snap2[2].text == "итак бюджет." and snap2[2].is_final
    assert all(a.end <= b.start for a, b in zip(snap2, snap2[1:]))   # common, ordered timeline


def test_backlog_window_commits_up_to_overlap_and_never_stalls():
    c = cfg()
    s1 = merge_window(LivePreviewState(), [W(1, 2, "a"), W(11, 11.9, "b")], 12.0, False, c)
    assert s1.committed_until == pytest.approx(2.0) or s1.committed_until >= 2.0
    assert not s1.tentative
    # a single word straddling the commit limit of a backlog window
    s2 = merge_window(LivePreviewState(committed_until=5.0), [W(5.5, 20.0, "длинное")], 17.0, False, c)
    assert s2.committed_until > 5.0


def test_new_words_after_uses_end_time_and_recent_text():
    ws = [W(9.7, 10.3, "четыре"), W(10.3, 10.9, "пять"), W(20, 20.5, "четыре")]
    out = new_words_after(ws, 10.4, [" три", " четыре"])
    assert [w.text.strip() for w in out] == ["пять", "четыре"]


def test_quiet_point_finds_pause():
    sr = 16000
    x = 0.3 * np.random.default_rng(0).standard_normal(sr * 4).astype(np.float32)
    x[int(2.2 * sr):int(2.4 * sr)] = 0
    assert 2.2 <= quiet_point(x, 2.0) <= 2.4


def test_spoken_phrases_are_preserved_but_no_speech_is_filtered():
    seg = lambda text, words, nsp=0.1: SimpleNamespace(text=text, no_speech_prob=nsp, words=words)  # noqa: E731
    res = SimpleNamespace(segments=[
        seg("Продолжение следует...", [SimpleNamespace(start=0, end=1, text=" Продолжение")]),
        seg("Тишина", [SimpleNamespace(start=1, end=2, text=" Тишина")], nsp=0.9),
        seg("Добрый день", [SimpleNamespace(start=2, end=3, text=" Добрый")]),
    ])
    assert [w.text for w in _words_of(res, 10.0, 0.6)] == [" Продолжение", " Добрый"]


# ---------------------------------------------------------------- transcribe_preview with fake ASR


class FakeWhisper:
    """Emits one word per 2 s of clip; real ASR quality is tested elsewhere."""

    def __init__(self, fail_on_call=None):
        self.calls, self.kwargs, self.fail_on_call = [], [], fail_on_call

    def transcribe(self, clip, **kw):
        n = len(clip) / 16000
        self.calls.append(n)
        self.kwargs.append(kw)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("decoder crashed")
        words = [SimpleNamespace(start=t + 0.2, end=t + 1.0, word=f" w{int(t)}") for t in np.arange(0, n - 1, 2.0)]
        seg = SimpleNamespace(start=0.0, end=n, text=" ".join(w.word for w in words), words=words, no_speech_prob=0.05)
        return iter([seg] if words else []), SimpleNamespace(language="ru", language_probability=0.9)

    def detect_language(self, clip, **kw):
        # Kazakh speech often scores as Tatar: must be restricted to ASR_ALLOWED_LANGUAGES
        return "tt", 0.6, [("tt", 0.6), ("kk", 0.3), ("ru", 0.1)]


class Reg:
    def __init__(self, model=None, missing=False):
        self.model, self.missing = model or FakeWhisper(), missing
        self.keys = []

    def get(self, key):
        self.keys.append(key)
        if self.missing:
            raise ModelUnavailable("asr", "нет весов faster-whisper")
        return self.model


@pytest.fixture(scope="module")
def webm(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("live") / "meeting.webm"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(FIX / "meeting_ru_kk.wav"),
                    "-c:a", "libopus", "-b:a", "32k", "-cluster_time_limit", "1000", "-f", "webm", str(out)], check=True)
    return out


def snap(tmp_path, webm, fraction):
    data = webm.read_bytes()
    n = int(len(data) * fraction)
    p = tmp_path / f"snap-{n}.webm"
    p.write_bytes(data[:n])  # arbitrary cut: truncated last cluster
    return p, n


def req(path, n, tmp_path, prev=None, cancelled=lambda: False):
    return LivePreviewRequest(path, "audio/webm;codecs=opus", n, tmp_path / "ai", prev, cancelled)


def test_truncated_container_prefixes_grow_monotonically(tmp_path, monkeypatch, webm):
    monkeypatch.setenv("LIVE_PREVIEW_REFINE_LANGUAGES", "false")
    reg = Reg()
    prev = None
    seen = []
    for frac in (0.001, 0.2, 0.45, 0.7, 1.0):
        p, n = snap(tmp_path, webm, frac)
        prev = transcribe_preview(req(p, n, tmp_path, prev), registry=reg)
        assert prev.preview_status in ("waiting", "ready") and prev.preview_error is None
        seen.append((prev.decoded_seconds, prev.processed_until_seconds, len(prev.utterances)))
    assert seen[0][0] == 0.0 and seen[0][2] == 0                    # header-only prefix: nothing, no error
    decoded = [d for d, _, _ in seen]
    assert decoded == sorted(decoded) and decoded[-1] == pytest.approx(31.4, abs=0.3)
    assert seen[1][2] > 0                                           # text before the last chunk
    assert max(reg.model.calls) <= 24.0 + 1e-6                      # never the whole meeting at once


def test_missing_model_is_unavailable_and_keeps_previous(tmp_path, webm):
    p, n = snap(tmp_path, webm, 0.5)
    ok = transcribe_preview(req(p, n, tmp_path), registry=Reg())
    p2, n2 = snap(tmp_path, webm, 0.9)
    bad = transcribe_preview(req(p2, n2, tmp_path, ok), registry=Reg(missing=True))
    assert bad.preview_status == "unavailable" and bad.preview_error["code"] == "MODEL_UNAVAILABLE"
    assert bad.utterances == ok.utterances and bad.processed_until_seconds == ok.processed_until_seconds


def test_busy_compute_lock_gives_waiting(tmp_path, webm):
    p, n = snap(tmp_path, webm, 0.5)
    with compute_lock:
        r = transcribe_preview(req(p, n, tmp_path), registry=Reg())
    assert r.preview_status == "waiting" and r.utterances == []


def test_cancel_raises(tmp_path, webm):
    p, n = snap(tmp_path, webm, 0.5)
    with pytest.raises(AIError) as exc:
        transcribe_preview(req(p, n, tmp_path, cancelled=lambda: True), registry=Reg())
    assert exc.value.code == "CANCELLED"
    assert compute_lock.acquire(blocking=False)  # released after the exception
    compute_lock.release()


def test_not_audio_snapshot_is_waiting_not_crash(tmp_path):
    p = tmp_path / "junk.webm"
    p.write_bytes(b"\x00" * 5000)
    r = transcribe_preview(req(p, 5000, tmp_path), registry=Reg())
    assert r.preview_status == "waiting" and r.utterances == []


def test_state_roundtrips_through_json(tmp_path, webm):
    p, n = snap(tmp_path, webm, 0.6)
    r = transcribe_preview(req(p, n, tmp_path), registry=Reg())
    import json

    back = LivePreviewResult.from_dict(json.loads(json.dumps(r.to_dict(), ensure_ascii=False)))
    assert back == r


# ---------------------------------------------------------------- incremental delivery (P0)


def small_windows(monkeypatch, window=8, max_windows=10):
    monkeypatch.setenv("LIVE_PREVIEW_WINDOW_SECONDS", str(window))
    monkeypatch.setenv("LIVE_PREVIEW_MAX_WINDOWS_PER_CALL", str(max_windows))
    monkeypatch.delenv("LIVE_ASR_MODEL_PATH", raising=False)
    monkeypatch.setenv("LIVE_PREVIEW_MAX_HOLES_PER_WINDOW", "0")   # fake words are sparse; holes tested below


def test_every_window_is_published_before_the_next_one_is_transcribed(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    reg = Reg()
    seen = []

    def on_update(r):
        seen.append((len(reg.model.calls), r))

    p, n = snap(tmp_path, webm, 1.0)
    last = transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", on_update=on_update),
                              registry=reg)
    assert len(seen) == len(reg.model.calls) >= 4
    assert [k for k, _ in seen] == list(range(1, len(seen) + 1))   # one callback per window, immediately
    assert seen[0][1].utterances                                    # first text after the FIRST window
    assert seen[-1][1] == last and not last.has_pending_audio
    ends = [r.processed_until_seconds for _, r in seen]
    assert ends == sorted(ends) and all(r.preview_status == "ready" for _, r in seen)
    assert seen[0][1].has_pending_audio                             # snapshot still holds backlog
    # every callback is a FULL snapshot: earlier final utterances are repeated unchanged
    first_final = [u for u in seen[1][1].utterances if u.is_final]
    assert all(u in last.utterances for u in first_final)


def test_backlog_is_drained_from_the_same_snapshot_without_new_chunks(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch, window=8, max_windows=1)
    reg = Reg()
    p, n = snap(tmp_path, webm, 1.0)
    r, calls = None, 0
    while True:
        r = transcribe_preview(req(p, n, tmp_path, r), registry=reg)
        calls += 1
        assert r.retry_after_ms is None
        if not r.has_pending_audio:
            break
        assert calls < 20
    assert calls >= 4                                       # 31 s in 8 s windows, one per call
    assert r.processed_until_seconds == pytest.approx(r.decoded_seconds, abs=0.05)
    # same snapshot again: not enough new audio -> waiting, no pending, no retry, text kept
    again = transcribe_preview(req(p, n, tmp_path, r), registry=reg)
    assert again.preview_status == "waiting" and not again.has_pending_audio and again.retry_after_ms is None
    assert again.utterances == r.utterances
    # windows never restart from zero: each starts near the previous committed point
    assert sum(reg.model.calls) < r.decoded_seconds + calls * 3.5


def test_busy_model_asks_to_retry_but_missing_audio_does_not(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    p, n = snap(tmp_path, webm, 0.5)
    with compute_lock:
        busy = transcribe_preview(req(p, n, tmp_path), registry=Reg())
    assert busy.preview_status == "waiting" and busy.retry_after_ms and busy.retry_after_ms > 0
    hdr, hn = snap(tmp_path, webm, 0.001)
    empty = transcribe_preview(req(hdr, hn, tmp_path), registry=Reg())
    assert empty.preview_status == "waiting" and empty.retry_after_ms is None and not empty.has_pending_audio


class SaveFailed(Exception):
    pass


def test_callback_errors_propagate_and_are_not_asr_errors(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    p, n = snap(tmp_path, webm, 1.0)

    def on_update(r):
        raise SaveFailed("db down")

    with pytest.raises(SaveFailed):
        transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", on_update=on_update),
                           registry=Reg())
    assert compute_lock.acquire(blocking=False)
    compute_lock.release()


def test_asr_failure_mid_call_keeps_windows_already_published(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    published = []
    p, n = snap(tmp_path, webm, 1.0)
    r = transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", on_update=published.append),
                           registry=Reg(FakeWhisper(fail_on_call=3)))
    assert r.preview_status == "unavailable" and r.preview_error["code"] == "ASR_FAILED"
    assert len(published) == 2 and r.utterances == published[-1].utterances
    assert r.state == published[-1].state and r.processed_until_seconds == published[-1].processed_until_seconds


def test_cancel_between_windows_raises_after_published_windows(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    published = []
    p, n = snap(tmp_path, webm, 1.0)
    with pytest.raises(AIError) as exc:
        transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", on_update=published.append,
                                              is_cancelled=lambda: len(published) >= 1), registry=Reg())
    assert exc.value.code == "CANCELLED" and len(published) == 1


def test_meeting_language_is_kept_and_auto_is_restricted_to_allowed(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    monkeypatch.setenv("ASR_ALLOWED_LANGUAGES", "ru,kk,en")
    p, n = snap(tmp_path, webm, 0.5)
    for choice, expected in (("kk", "kk"), ("ru", "ru"), ("auto", "kk")):
        reg = Reg()
        transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", asr_language=choice),
                           registry=reg)
        assert {kw["language"] for kw in reg.model.kwargs} == {expected}, choice


def test_old_state_without_new_fields_continues_after_last_final(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    old = {"processed_until_seconds": 20.0, "preview_status": "ready",
           "utterances": [{"id": "preview-0", "start": 0.5, "end": 4.4, "speaker_label": None,
                           "text": "Коллеги, начинаем.", "is_final": True},
                          {"id": "preview-1", "start": 4.9, "end": 12.0, "speaker_label": None,
                           "text": "черновик", "is_final": False}]}          # no "state", no new fields
    prev = LivePreviewResult.from_dict(old)
    assert prev.has_pending_audio is False and prev.retry_after_ms is None
    reg = Reg()
    p, n = snap(tmp_path, webm, 1.0)
    published = []
    r = transcribe_preview(LivePreviewRequest(p, "audio/webm", n, tmp_path / "ai", prev,
                                              on_update=published.append), registry=reg)
    assert r.utterances[0] == prev.utterances[0]                  # final kept, id stable
    assert all(u.id != "preview-0" for u in r.utterances[1:])
    assert published[0].processed_until_seconds > 4.4 + 8.0 - 1.0  # first window starts near 4.4 s, not at 0
    assert r.processed_until_seconds > 20.0


def test_shorter_snapshot_than_already_decoded_is_diagnosed(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    p, n = snap(tmp_path, webm, 1.0)
    full = transcribe_preview(req(p, n, tmp_path), registry=Reg())
    p2, n2 = snap(tmp_path, webm, 0.3)                              # e.g. one chunk sent as a file
    bad = transcribe_preview(req(p2, n2, tmp_path, full), registry=Reg())
    assert bad.preview_status == "unavailable" and bad.preview_error["code"] == "AUDIO_INVALID"
    assert bad.utterances == full.utterances and bad.state == full.state


def test_only_stable_bytes_are_read(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch)
    data = webm.read_bytes()
    p = tmp_path / "growing.webm"
    p.write_bytes(data)                                             # file already longer than stable_bytes
    r = transcribe_preview(req(p, len(data) // 2, tmp_path), registry=Reg())
    assert 10.0 < r.decoded_seconds < 20.0


def test_configured_live_model_is_used_and_never_silently_replaced(tmp_path, monkeypatch, webm):
    from app.config import Settings
    from app.ml import ModelRegistry

    small_windows(monkeypatch)
    p, n = snap(tmp_path, webm, 0.5)
    monkeypatch.setenv("LIVE_ASR_MODEL_PATH", "faster-whisper-large-v3-turbo")
    reg = Reg()
    r = transcribe_preview(req(p, n, tmp_path), registry=reg)
    assert set(reg.keys) == {"live_asr"} and r.asr_model == "live_asr"
    monkeypatch.setenv("LIVE_ASR_MODEL_PATH", str(tmp_path / "no-such-model"))
    real = ModelRegistry(Settings(models_dir=tmp_path))
    bad = transcribe_preview(req(p, n, tmp_path), registry=real)
    assert bad.preview_status == "unavailable" and bad.preview_error["code"] == "MODEL_UNAVAILABLE"
    assert "no-such-model" in bad.preview_error["message"]
    assert real.slots["asr"].status == "not_loaded" and real.slots["live_asr"].status == "error"
    monkeypatch.delenv("LIVE_ASR_MODEL_PATH")
    reg2 = Reg()
    transcribe_preview(req(p, n, tmp_path), registry=reg2)
    assert set(reg2.keys) == {"asr"}
    assert ModelRegistry(Settings(models_dir=tmp_path)).status()["live_asr"]["status"] == "not_configured"


def test_new_result_fields_roundtrip():
    r = LivePreviewResult(processed_until_seconds=3.0, utterances=[], has_pending_audio=True, retry_after_ms=1500,
                          asr_model="live_asr")
    assert LivePreviewResult.from_dict(r.to_dict()) == r


def test_voiced_audio_without_words_is_found_for_redecoding():
    words = [W(0.5, 1.0, "Раз"), W(1.1, 2.0, "два.")]
    voiced = [(0.4, 2.1), (2.8, 4.6), (5.0, 5.4), (9.0, 12.0)]
    # (2.8-4.6): a dropped phrase; (5.0-5.4): too short; (9-12): beyond the commit limit -> next window
    assert find_holes(words, voiced, 0.0, 8.5, 0.8) == [(2.8, 4.6)]
    assert find_holes(words, voiced, 3.0, 8.5, 0.8) == [(3.0, 4.6)]      # committed audio is not redone
    assert find_holes(words + [W(2.9, 4.5, "три")], voiced, 0.0, 8.5, 0.8) == []


def test_hole_is_redecoded_and_words_are_merged(tmp_path, monkeypatch, webm):
    small_windows(monkeypatch, window=12)
    monkeypatch.setenv("LIVE_PREVIEW_MAX_HOLES_PER_WINDOW", "2")

    class Deaf(FakeWhisper):
        """Window decode returns nothing; the isolated hole decode (vad_filter=False) returns words."""

        def transcribe(self, clip, **kw):
            it, info = super().transcribe(clip, **kw)
            return (it if kw.get("vad_filter") is False else iter([])), info

    reg = Reg(Deaf())
    p, n = snap(tmp_path, webm, 0.5)
    r = transcribe_preview(req(p, n, tmp_path), registry=reg)
    assert any(kw.get("vad_filter") is False for kw in reg.model.kwargs)
    assert r.utterances and all(a.end <= b.start + 1e-6 for a, b in zip(r.utterances, r.utterances[1:]))


def test_long_meeting_decodes_only_the_tail_on_the_common_timeline(tmp_path, monkeypatch):
    small_windows(monkeypatch, window=12, max_windows=40)
    long = tmp_path / "long.webm"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-stream_loop", "3", "-i",
                    str(FIX / "meeting_ru_kk.wav"), "-c:a", "libopus", "-b:a", "32k", "-cluster_time_limit", "1000",
                    "-f", "webm", str(long)], check=True)
    data = long.read_bytes()
    first = tmp_path / "first.webm"
    first.write_bytes(data[:int(len(data) * 0.7)])

    def run(margin):
        monkeypatch.setenv("LIVE_PREVIEW_TAIL_DECODE_MARGIN_SECONDS", str(margin))
        reg = Reg()
        r = transcribe_preview(req(first, first.stat().st_size, tmp_path), registry=reg)
        r = transcribe_preview(req(long, len(data), tmp_path, r), registry=reg)
        return r, reg.model.calls

    tail, tail_calls = run(30)
    full, full_calls = run(0)
    assert tail.decoded_seconds == pytest.approx(full.decoded_seconds, abs=0.1)
    assert tail.processed_until_seconds == pytest.approx(full.processed_until_seconds, abs=0.1)
    assert len(tail_calls) == len(full_calls)
    assert [round(u.start) for u in tail.utterances] == [round(u.start) for u in full.utterances]
