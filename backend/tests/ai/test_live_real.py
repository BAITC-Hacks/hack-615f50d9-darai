"""Live preview on the REAL faster-whisper model with a WebM/Opus container fed in parts.

Not mocked; skipped without weights. Fixtures are simulated TTS speech (live_long:
negations, a repeated phrase, a long pause, KZ/mixed turns) - not a quality claim
for real meetings. The live model follows the env (LIVE_ASR_MODEL_PATH, e.g.
faster-whisper-large-v3-turbo); the final pipeline must still use "asr" (large-v3).
Run: MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 [LIVE_ASR_MODEL_PATH=faster-whisper-large-v3-turbo] \
     pytest tests/ai/test_live_real.py -s
"""

import importlib.util
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.ai_types import AIError, MeetingContext, ProcessRequest
from app.config import Settings

ROOT = Path(__file__).resolve().parents[3]
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ai"
MAN = json.loads((FIX / "manifest.json").read_text())
S = Settings()
pytestmark = [pytest.mark.real_models,
              pytest.mark.skipif(not (S.resolved_asr_path / "model.bin").is_file(), reason="no local ASR weights")]


def _sim():
    spec = importlib.util.spec_from_file_location("live_simulate", ROOT / "scripts" / "ai" / "live_simulate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["live_simulate"] = mod
    spec.loader.exec_module(mod)
    return mod


def norm(s):
    return re.sub(r"[^\w ]+", "", s.lower().replace("ё", "е"))


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    return _sim().simulate([FIX / "live_long.wav", FIX / "meeting_ru_kk.wav"], chunk_seconds=2.0,
                           every_seconds=4.0, work=tmp_path_factory.mktemp("live"), language="auto")


def test_text_appears_before_last_chunk(run):
    print("\n" + "\n".join(f"  {c}" for c in run["calls"]))
    print("  metrics:", {k: v for k, v in run["metrics"].items() if k not in ("per_turn", "latency")})
    assert run["first_text"]["audio_received"] < 10.0
    assert run["metrics"]["windows"] >= len(run["calls"]) - 2     # published window by window


def test_negations_and_repeated_phrase_survive(run):
    full = norm(" ".join(u.text for u in run["result"].utterances))
    for t in run["metrics"]["per_turn"]:
        print(f"  {t['cer']:.3f} [{t['lang']}] {t['hyp']}")
    assert "я буду готовить отчет" in full and "я не буду готовить презентацию" in full
    assert "марат не будет" in full
    assert full.count("коллеги начинаем планерку") == 2          # same phrase, different time: both kept


def test_no_duplicates_no_lost_sentences_common_timeline(run):
    utts = run["result"].utterances
    for u in utts:
        print(f"  {u.id} [{u.start:6.2f}-{u.end:6.2f}] {'F' if u.is_final else '~'} {u.text}")
    full = norm(" ".join(u.text for u in utts))
    for phrase in ("начинаем совещание по бюджету", "подготовьте пожалуйста финансовый отчет до пятницы",
                   "нужно обновить презентацию", "этим займусь я сама", "согласовать график поставок"):
        assert full.count(phrase) == 1, phrase
    assert full.count("совещание окончено") == 2                  # once in each concatenated recording
    assert all(a.end <= b.start + 1e-6 for a, b in zip(utts, utts[1:]))
    assert len({u.id for u in utts}) == len(utts)
    assert utts[-1].end <= run["duration"] + 0.1
    assert run["result"].processed_until_seconds == pytest.approx(run["duration"], abs=0.3)


def test_silence_creates_no_utterances(tmp_path):
    r = _sim().simulate(FIX / "silence.wav", chunk_seconds=1.0, every_seconds=2.0, work=tmp_path)
    assert r["result"].utterances == []


def test_finalization_uses_full_pipeline(run, tmp_path):
    from app.ai_pipeline import AIPipeline
    from app.ml import ModelRegistry

    class Spy(ModelRegistry):
        keys: list = []

        def get(self, key):
            self.keys.append(key)
            return super().get(key)

    spy = Spy(S)
    stages = []
    req = ProcessRequest(source_path=Path(run["container"]), work_dir=tmp_path / "final",
                         meeting=MeetingContext(datetime(2026, 9, 23, 10, tzinfo=ZoneInfo("Asia/Almaty")),
                                                "Asia/Almaty", []),
                         candidates=[], on_stage=stages.append, run_extraction=False)
    diar_ready = S.resolved_diarization_config.is_file() and (S.resolved_segmentation_path / "pytorch_model.bin").is_file()
    if diar_ready:
        res = AIPipeline(settings=S, registry=spy).process_recording(req)
        assert res.utterances and res.speakers
    else:
        with pytest.raises(AIError) as exc:
            AIPipeline(settings=S, registry=spy).process_recording(req)
        assert (exc.value.code, exc.value.stage, exc.value.model) == ("MODEL_UNAVAILABLE", "diarizing", "diarization")
        assert stages[:3] == ["normalizing", "transcribing", "diarizing"]
    # the final transcript comes from the "asr" slot = large-v3, never from the live model
    assert "asr" in spy.keys and "live_asr" not in spy.keys
    assert S.resolved_asr_path.name == "faster-whisper-large-v3"
