"""Pipeline orchestration with FAKE models (real FFmpeg, real alignment/matching code).

Checks stage order, cancellation, typed errors, "unknown" speakers and that
an LLM failure keeps the transcript. Model quality: test_real_models.py.
"""

import json
import uuid
import wave
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest

from app.ai_pipeline import AIPipeline
from app.ai_types import AIError, MeetingContext, Participant, ProcessRequest, VoiceCandidate
from app.asr import pick_language
from app.config import Settings
from app.llm import LLMClient
from app.ml import ModelRegistry

SR = 16000
ANNA, DANA = uuid.UUID(int=1), uuid.UUID(int=2)
VA = np.eye(256)[0]
VB = np.eye(256)[1]
MEETING = MeetingContext(datetime(2026, 9, 23, 10, tzinfo=ZoneInfo("Asia/Almaty")), "Asia/Almaty",
                         [Participant(ANNA, "Иванова Анна"), Participant(DANA, "Ахметова Дана")])


def write_wav(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return path


def noise(seconds: float, seed=0) -> np.ndarray:
    return 0.2 * np.random.default_rng(seed).standard_normal(int(seconds * SR))


def bursts(seconds: float) -> np.ndarray:
    """Speech-like on/off energy (1.2 s on, 0.3 s off) so the enrollment VAD finds regions."""
    t = np.arange(int(seconds * SR)) / SR
    return noise(seconds) * ((t % 1.5) < 1.2)


class FakeWhisper:
    """Two speakers: A 0-4 s, B 4.5-8 s."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        assert kw["word_timestamps"] and kw["task"] == "transcribe"
        self.calls += 1
        w = lambda s, e, t: SimpleNamespace(start=s, end=e, word=t)  # noqa: E731
        segs = [SimpleNamespace(start=0.0, end=4.0, text="Дана, подготовьте отчёт.",
                                words=[w(0.1, 0.6, " Дана,"), w(0.7, 2.0, " подготовьте"), w(2.1, 3.9, " отчёт.")]),
                SimpleNamespace(start=4.5, end=8.0, text="Жарайды.", words=[w(4.6, 7.5, " Жарайды.")])]
        return iter(segs), SimpleNamespace(language="ru", language_probability=0.9)

    def detect_language(self, clip):
        return "ru", 0.9, [("ru", 0.9), ("kk", 0.05)]


class FakeDiarization:
    def __call__(self, audio, **kw):
        from pyannote.core import Annotation, Segment

        assert audio["sample_rate"] == SR and audio["waveform"].shape[0] == 1
        ann = Annotation()
        ann[Segment(0.0, 4.2)] = "SPEAKER_00"
        ann[Segment(4.3, 8.0)] = "SPEAKER_01"
        return ann


class FakeEmbedding:
    """Vector by time: first 4.25 s -> VA-ish, later -> unregistered voice."""

    def crop(self, audio, seg):
        return VA * 0.9 + VB * 0.1 if seg.end <= 4.25 else np.eye(256)[5]


class FakeRegistry:
    def __init__(self, missing=()):
        self.models = {"asr": FakeWhisper(), "diarization": FakeDiarization(), "embedding": FakeEmbedding()}
        self.missing = set(missing)

    def get(self, key):
        from app.ml import ModelUnavailable

        if key in self.missing:
            raise ModelUnavailable(key, f"нет весов {key}")
        return self.models[key]

    def status(self):
        return {k: {"status": "ready", "model_id": k, "error": None} for k in self.models}


def llm_client(s, ok=True):
    def handler(request):
        if not ok:
            return httpx.Response(503)
        body = json.loads(request.content)
        if "саммари" in body["messages"][0]["content"]:
            content = {"summary": "Кратко.", "decisions": [], "open_questions": []}
        else:
            content = {"tasks": [{"task": "Подготовить отчёт", "assignee_id": str(DANA), "deadline": None,
                                  "deadline_text": None, "evidence": "подготовьте отчёт",
                                  "source_utterance_ids": [0], "confidence": 0.8}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})

    return LLMClient(s, transport=httpx.MockTransport(handler))


def make(tmp_path, *, missing=(), llm_ok=True, **settings_kw):
    s = Settings(llm_model="qwen3:8b", llm_base_url="http://localhost:11434/v1", data_dir=tmp_path,
                 models_dir=tmp_path / "no-models", **settings_kw)
    return AIPipeline(settings=s, registry=FakeRegistry(missing), llm_client=llm_client(s, llm_ok))


def request(tmp_path, src, stages, candidates=(), cancel_after=None):
    def on_stage(stage):
        stages.append(stage)

    def cancelled():
        return cancel_after is not None and cancel_after in stages

    return ProcessRequest(source_path=src, work_dir=tmp_path / "rec", meeting=MEETING,
                          candidates=list(candidates), on_stage=on_stage, is_cancelled=cancelled)


def test_full_pipeline_with_fakes(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(8.5))
    stages: list[str] = []
    cands = [VoiceCandidate(ANNA, list(VA)), VoiceCandidate(DANA, list(VB))]
    res = make(tmp_path).process_recording(request(tmp_path, src, stages, cands))
    assert stages == ["normalizing", "transcribing", "diarizing", "aligning", "identifying", "extracting"]
    assert res.normalized_path == (tmp_path / "rec" / "normalized.wav").resolve() and res.normalized_path.is_file()
    assert abs(res.duration_seconds - 8.5) < 0.05
    assert [(u.speaker_label, u.text) for u in res.utterances] == [
        ("SPEAKER_00", "Дана, подготовьте отчёт."), ("SPEAKER_01", "Жарайды.")]
    sp = {s.label: s for s in res.speakers}
    assert sp["SPEAKER_00"].proposed_employee_id == ANNA and not sp["SPEAKER_00"].review_required
    assert sp["SPEAKER_01"].proposed_employee_id is None  # unregistered voice -> unknown
    assert "below_threshold" in sp["SPEAKER_01"].review_reasons
    assert res.extraction.status == "ok" and res.extraction.tasks[0].to_employee_id == DANA
    assert res.extraction.tasks[0].from_employee_id == ANNA
    assert [t.stage for t in res.timings] == stages


def test_no_candidates_skips_embedding(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(8.5))
    p = make(tmp_path, missing=("embedding",))
    res = p.process_recording(request(tmp_path, src, []))
    assert all(s.review_reasons == ["no_candidates"] for s in res.speakers)


def test_llm_failure_keeps_transcript(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(8.5))
    res = make(tmp_path, llm_ok=False).process_recording(request(tmp_path, src, []))
    assert res.utterances and res.extraction.status == "error"
    assert res.extraction.error_code == "LLM_UNAVAILABLE" and res.extraction.tasks == []


def test_silence_is_audio_empty(tmp_path):
    src = write_wav(tmp_path / "in.wav", np.zeros(SR * 3))
    with pytest.raises(AIError) as exc:
        make(tmp_path).process_recording(request(tmp_path, src, []))
    assert exc.value.code == "AUDIO_EMPTY" and exc.value.stage == "normalizing"


def test_not_audio_is_invalid(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"definitely not audio" * 100)
    with pytest.raises(AIError) as exc:
        make(tmp_path).process_recording(request(tmp_path, src, []))
    assert exc.value.code == "AUDIO_INVALID"


def test_too_long(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(3))
    with pytest.raises(AIError) as exc:
        make(tmp_path, max_audio_seconds=2).process_recording(request(tmp_path, src, []))
    assert exc.value.code == "AUDIO_TOO_LONG"


@pytest.mark.parametrize("missing,stage", [("asr", "transcribing"), ("diarization", "diarizing")])
def test_missing_model_is_typed_error(tmp_path, missing, stage):
    src = write_wav(tmp_path / "in.wav", noise(8.5))
    with pytest.raises(AIError) as exc:
        make(tmp_path, missing=(missing,)).process_recording(request(tmp_path, src, []))
    assert (exc.value.code, exc.value.stage, exc.value.model) == ("MODEL_UNAVAILABLE", stage, missing)


def test_cancellation_between_stages(tmp_path):
    src = write_wav(tmp_path / "in.wav", noise(8.5))
    stages: list[str] = []
    with pytest.raises(AIError) as exc:
        make(tmp_path).process_recording(request(tmp_path, src, stages, cancel_after="diarizing"))
    assert exc.value.code == "CANCELLED" and stages[-1] == "diarizing"


def test_real_registry_without_weights_reports_unavailable(tmp_path):
    """No stubs: missing weights -> MODEL_UNAVAILABLE with the path, status shows the error."""
    s = Settings(models_dir=tmp_path / "empty", llm_model="x")
    reg = ModelRegistry(s)
    p = AIPipeline(settings=s, registry=reg, llm_client=LLMClient(s))
    src = write_wav(tmp_path / "in.wav", noise(2))
    with pytest.raises(AIError) as exc:
        p.process_recording(request(tmp_path, src, []))
    assert exc.value.code == "MODEL_UNAVAILABLE" and "faster-whisper" in exc.value.message
    assert reg.status()["asr"]["status"] == "error"
    with pytest.raises(AIError) as exc:
        p.enroll_voice(write_wav(tmp_path / "e.wav", bursts(20)), tmp_path / "enr")
    assert exc.value.code == "MODEL_UNAVAILABLE"


def test_voice_model_info_without_loading(tmp_path):
    s = Settings(models_dir=tmp_path)
    d = s.resolved_embedding_path
    d.mkdir(parents=True)
    (d / "REVISION").write_text("pyannote/wespeaker-voxceleb-resnet34-LM@abc123\n")
    reg = ModelRegistry(s)
    info = AIPipeline(settings=s, registry=reg, llm_client=LLMClient(s)).voice_model_info()
    assert (info.model_id, info.revision, info.dimension) == ("pyannote/wespeaker-voxceleb-resnet34-LM", "abc123", 256)
    assert reg.status()["embedding"]["status"] == "not_loaded"


def test_pick_language_restricted_to_allowed():
    probs = [("tt", 0.5), ("kk", 0.3), ("ru", 0.1), ("en", 0.05)]
    lang, p = pick_language(probs, ["ru", "kk", "en"])
    assert lang == "kk" and abs(p - 0.3 / 0.45) < 1e-9
    assert pick_language([("tt", 1.0)], ["ru"]) == (None, 0.0)
