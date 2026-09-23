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

    def __init__(self):
        self.calls = []

    def transcribe(self, clip, **kw):
        n = len(clip) / 16000
        self.calls.append(n)
        words = [SimpleNamespace(start=t + 0.2, end=t + 1.0, word=f" w{int(t)}") for t in np.arange(0, n - 1, 2.0)]
        seg = SimpleNamespace(start=0.0, end=n, text=" ".join(w.word for w in words), words=words, no_speech_prob=0.05)
        return iter([seg] if words else []), SimpleNamespace(language="ru", language_probability=0.9)

    def detect_language(self, clip):
        return "ru", 0.9, [("ru", 0.9)]


class Reg:
    def __init__(self, model=None, missing=False):
        self.model, self.missing = model or FakeWhisper(), missing

    def get(self, key):
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
