"""Live preview on the REAL faster-whisper model with a WebM/Opus container fed in parts.

Not mocked; skipped without weights. Fixtures are simulated TTS speech.
Run: MODELS_DIR=../models ASR_COMPUTE_TYPE=int8 pytest tests/ai/test_live_real.py -s
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
    return _sim().simulate(FIX / "meeting_ru_kk.wav", chunk_seconds=2.0, every_seconds=4.0,
                           work=tmp_path_factory.mktemp("live"))


def test_text_appears_before_last_chunk(run):
    print("\n" + "\n".join(f"  {c}" for c in run["calls"]))
    assert run["first_text"]["chunk"] < run["chunks"] - 1


def test_no_duplicates_no_lost_sentences_common_timeline(run):
    utts = run["result"].utterances
    for u in utts:
        print(f"  {u.id} [{u.start:6.2f}-{u.end:6.2f}] {'F' if u.is_final else '~'} {u.text}")
    full = norm(" ".join(u.text for u in utts))
    for phrase in ("начинаем совещание по бюджету", "подготовьте пожалуйста финансовый отчет до пятницы",
                   "нужно обновить презентацию", "этим займусь я сама", "совещание окончено"):
        assert full.count(phrase) == 1, phrase
    assert all(a.end <= b.start + 1e-6 for a, b in zip(utts, utts[1:]))
    assert len({u.id for u in utts}) == len(utts)
    assert utts[-1].end <= MAN["recordings"]["meeting_ru_kk"]["duration"] + 0.1
    assert run["result"].processed_until_seconds == pytest.approx(run["duration"], abs=0.3)


def test_silence_creates_no_utterances(tmp_path):
    r = _sim().simulate(FIX / "silence.wav", chunk_seconds=1.0, every_seconds=2.0, work=tmp_path)
    assert r["result"].utterances == []


def test_finalization_uses_full_pipeline(run, tmp_path):
    from app.ai_pipeline import AIPipeline

    stages = []
    req = ProcessRequest(source_path=Path(run["container"]), work_dir=tmp_path / "final",
                         meeting=MeetingContext(datetime(2026, 9, 23, 10, tzinfo=ZoneInfo("Asia/Almaty")),
                                                "Asia/Almaty", []),
                         candidates=[], on_stage=stages.append, run_extraction=False)
    diar_ready = S.resolved_diarization_config.is_file() and (S.resolved_segmentation_path / "pytorch_model.bin").is_file()
    if diar_ready:
        res = AIPipeline(settings=S).process_recording(req)
        assert res.utterances and res.speakers
    else:
        with pytest.raises(AIError) as exc:
            AIPipeline(settings=S).process_recording(req)
        assert (exc.value.code, exc.value.stage, exc.value.model) == ("MODEL_UNAVAILABLE", "diarizing", "diarization")
        assert stages[:3] == ["normalizing", "transcribing", "diarizing"]
