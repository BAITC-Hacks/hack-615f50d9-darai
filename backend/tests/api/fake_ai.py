"""TEST-ONLY adapter for the AI pipeline interface (docs/AI_CONTRACT.md).

Returns scripted results so backend tests can check persistence, access
rules and state transitions. It performs no speech recognition and must never
be wired into the runtime (it is only installed via ai_gateway.set_test_adapter
in tests/api/conftest.py).
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from app import ai_types
from app.audio import normalize


class TestAIAdapter:
    __test__ = False  # not a pytest class
    types = ai_types

    def __init__(self) -> None:
        self.speaker_map: dict[str, uuid.UUID | None] = {}
        self.fail_with: ai_types.AIError | None = None
        self.extraction_error: str | None = None
        self.tasks: list[ai_types.TaskResult] | None = None
        self.calls: list[tuple[str, object]] = []
        self.stages: list[str] = []
        self.enroll_reject = False
        self.hook = None  # callable run inside process_recording (e.g. simulate a retry race)

    # -- interface -----------------------------------------------------

    def voice_model_info(self) -> ai_types.VoiceModelInfo:
        return ai_types.VoiceModelInfo(model_id="test/wespeaker", revision="t1", dimension=4)

    def models_status(self) -> dict:
        return {k: {"status": "ready", "model_id": "test", "error": None} for k in ("asr", "diarization", "embedding")}

    def preload(self) -> None:
        pass

    def enroll_voice(self, source_path: Path, work_dir: Path) -> ai_types.EnrollmentResult:
        self.calls.append(("enroll", source_path))
        if self.enroll_reject:
            raise ai_types.VoiceQualityRejected(["too_short"], 3.0, 15.0, "Слишком мало чистой речи")
        return ai_types.EnrollmentResult(vector=[0.5, 0.5, 0.5, 0.5], model=self.voice_model_info(),
                                         quality_status="ok", reasons=[], speech_seconds=22.0)

    def process_recording(self, req: ai_types.ProcessRequest) -> ai_types.ProcessResult:
        self.calls.append(("process", req))
        for stage in ("normalizing", "transcribing", "diarizing", "aligning", "identifying", "extracting"):
            req.on_stage(stage)
            self.stages.append(stage)
            if self.hook and stage == "identifying":
                self.hook()
            if req.is_cancelled():
                raise ai_types.AIError("CANCELLED", "cancelled", stage=stage)
            if self.fail_with is not None and self.fail_with.stage == stage:
                raise self.fail_with
        out = req.work_dir / "normalized.wav"
        normalize(req.source_path, out, timeout=60)
        utterances = [
            ai_types.UtteranceResult(0, 0.0, 3.0, "SPEAKER_00", "Дана, подготовьте отчёт до пятницы.", "ru", []),
            ai_types.UtteranceResult(1, 3.5, 6.0, "SPEAKER_01", "Жақсы, жасаймын.", "kk", []),
            ai_types.UtteranceResult(2, 6.5, 8.0, "SPEAKER_02", "Кто-то должен проверить смету.", "ru",
                                     ["overlapped_speech"]),
        ]
        speakers = []
        for label in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02"):
            emp = self.speaker_map.get(label)
            speakers.append(ai_types.SpeakerResult(
                label=label, proposed_employee_id=emp, similarity=0.8 if emp else 0.31,
                second_similarity=0.2, review_required=emp is None,
                review_reasons=[] if emp else ["below_threshold"], clean_speech_seconds=5.0))
        return ai_types.ProcessResult(normalized_path=out, duration_seconds=8.0, languages=["ru", "kk"],
                                      speakers=speakers, utterances=utterances,
                                      extraction=self._extraction(req.meeting, [0, 1, 2]))

    def extract(self, lines, meeting) -> ai_types.ExtractionOutcome:
        self.calls.append(("extract", lines))
        return self._extraction(meeting, [l.id for l in lines])

    # -- helpers ---------------------------------------------------------

    def _extraction(self, meeting, ids: list[int]) -> ai_types.ExtractionOutcome:
        if self.extraction_error:
            return ai_types.ExtractionOutcome(status="error", error_code=self.extraction_error,
                                              error_message="LLM дважды вернула невалидный JSON")
        if self.tasks is not None:
            tasks = self.tasks
        else:
            dana = next((p.employee_id for p in meeting.participants if "Дана" in p.fio), None)
            tasks = [
                ai_types.TaskResult(task="Подготовить отчёт", to_employee_id=dana, from_label="SPEAKER_00",
                                    from_employee_id=None, deadline=date(2026, 9, 25), deadline_source="до пятницы",
                                    evidence="Дана, подготовьте отчёт до пятницы.", source_ids=[ids[0]],
                                    confidence=0.9, review_reasons=[]),
                ai_types.TaskResult(task="Проверить смету", to_employee_id=None, from_label="SPEAKER_02",
                                    from_employee_id=None, deadline=None, deadline_source=None,
                                    evidence="Кто-то должен проверить смету.", source_ids=[ids[2]],
                                    confidence=0.5,
                                    review_reasons=["missing_assignee", "missing_deadline", "low_confidence"]),
            ]
        return ai_types.ExtractionOutcome(status="ok", tasks=list(tasks), summary="Обсудили отчёт и смету.")
