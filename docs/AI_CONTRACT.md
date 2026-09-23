# DARAI — контракт Python-интерфейса AI-пайплайна

Статус: **согласовано 2026-09-23** (AI-агент и backend-агент), версия 1.0. Источник истины по полям — `backend/app/ai_types.py`. Изменения только по согласованию сторон с обновлением этого файла. Добавление новых значений `*_reasons` считается совместимым изменением.

Владение: AI-агент — `ai_types.py`, `ai_pipeline.py`, `asr.py`, `diarize.py`, `voice.py`, `align.py`, `extract.py`, `ml.py`, `llm.py`, `requirements-ml.txt`. Backend — `audio.py` (AI импортирует его как есть), `processing.py`, `ai_gateway.py`, `config.py`, БД, HTTP.

## Общие правила

- AI-модули не импортируют FastAPI, Starlette, SQLAlchemy, `app.models`, `app.db`. Разрешено: `app.config`, `app.audio`.
- Функции синхронные и блокирующие. Backend вызывает их вне event loop и сам ограничивает конкуренцию (одна тяжёлая задача). Модели грузятся один раз на процесс, потокобезопасно (`app.ml.ModelRegistry`).
- Ошибка обработки — исключение `AIError(code, message, stage, model)`. Фиктивного успешного результата нет: отсутствующая модель → `MODEL_UNAVAILABLE`.
- Ошибка LLM **не** исключение: `ExtractionOutcome(status="error", error_code=..., tasks=[], summary=None)`. Это отличается от `status="ok", tasks=[]`, когда поручений нет.
- Сеть: только `LLM_BASE_URL`, который должен разрешаться в loopback/private адрес; Ollama-модели `*:cloud` запрещены. Hub выключен (`HF_HUB_OFFLINE=1`).
- Логи содержат только названия этапов, длительности, счётчики и типы исключений. Текст, аудио, векторы и токены в логи не попадают. `AIError.message` показывается пользователю и не содержит содержимого совещания.
- ID сотрудников — `uuid.UUID`. Время — секунды `float` от начала записи. Даты — `datetime.date`.

## Функции (`app/ai_pipeline.py`)

```python
get_pipeline() -> AIPipeline                    # singleton; AIPipeline(settings, registry, llm_client) для тестов
process_recording(req: ProcessRequest) -> ProcessResult
extract(lines: list[TranscriptLine], meeting: MeetingContext) -> ExtractionOutcome
enroll_voice(source_path: Path, work_dir: Path) -> EnrollmentResult
voice_model_info() -> VoiceModelInfo            # без загрузки весов
models_status() -> dict[str, dict]              # без загрузки весов
preload() -> None                               # только при PRELOAD_MODELS=true; ошибки → models_status
```

| Функция | Поведение |
| --- | --- |
| `process_recording` | Этапы `normalizing → transcribing → diarizing → aligning → identifying → extracting`. Перед каждым этапом проверяется `is_cancelled()`, затем вызывается `on_stage(stage)`. Отмена → `AIError("CANCELLED")`. `normalized.wav` (16 kHz mono PCM) пишется в `work_dir`. При `run_extraction=False` извлечение пропускается: `extraction.status="not_started"`. |
| `extract` | Поручения и саммари по сохранённому транскрипту: повтор после ошибки LLM или после ручной правки спикеров. `TranscriptLine.id` — id реплик в БД, `TaskResult.source_ids` ссылается на них. |
| `enroll_voice` | Та же модель WeSpeaker и та же нарезка (куски `VOICE_ENROLL_CHUNK_SECONDS`, не короче `VOICE_MIN_SEGMENT_SECONDS`, L2 → среднее → L2), что при сопоставлении. Временный WAV удаляется; исходник удаляет backend. |
| `voice_model_info` | `model_id` = `VOICE_MODEL_ID`, `revision` = `VOICE_MODEL_REVISION` или, если там `local`, sha из `<EMBEDDING_MODEL_PATH>/REVISION` (пишет `prepare_models.py`), `dimension` = `VOICE_EMBEDDING_DIM`. Профиль совместим только при полном совпадении трёх полей. |
| `models_status` | `{"asr"|"diarization"|"embedding": {"status": "not_loaded|loading|ready|error", "model_id", "error"}}`. |

### Ошибки

`AIError.code`:

| code | Когда | stage |
| --- | --- | --- |
| `AUDIO_INVALID` | ffprobe не нашёл аудио, исходник отсутствует | `normalizing` |
| `AUDIO_EMPTY` | < 0.5 с или пиковая амплитуда < 1e-3; ASR не нашёл ни одного слова | `normalizing` / `transcribing` |
| `AUDIO_TOO_LONG` | > `MAX_AUDIO_SECONDS` | `normalizing` |
| `FFMPEG_FAILED` | ffmpeg упал или вышел по таймауту | `normalizing` |
| `MODEL_UNAVAILABLE` | нет весов, не импортируется библиотека, ошибка загрузки; `model` = `asr`, `diarization`, `embedding` или `device` | этап, где модель понадобилась |
| `ASR_FAILED`, `DIARIZATION_FAILED`, `EMBEDDING_FAILED` | исключение внутри модели; сообщение содержит только тип исключения | соответствующий |
| `CANCELLED` | `is_cancelled()` вернул True | этап, перед которым проверили |
| `INTERNAL_ERROR` | прочее | — |

`VoiceQualityRejected(reasons, speech_seconds, required_seconds, message)`: `reasons` ⊂ {`too_short`, `silence`, `clipping`}. Образец не сохраняется.

## Типы (`app/ai_types.py`)

Вход:

- `Participant(employee_id, fio, position="")` — все участники встречи; только они допустимы как исполнители.
- `VoiceCandidate(employee_id, vector)` — только приглашённые участники с совместимым профилем. Backend фильтрует по `voice_model_info()`; AI дополнительно отбрасывает векторы неверной размерности.
- `MeetingContext(starts_at_local, timezone, participants)` — aware datetime в часовом поясе встречи и IANA-имя. Относительные сроки считаются от даты `starts_at_local`.
- `ProcessRequest(source_path, work_dir, meeting, candidates, on_stage, is_cancelled, run_extraction=True)`.
- `TranscriptLine(id, speaker_label, employee_id, text, start)` — `employee_id` текущей привязки спикера или None.

Выход:

- `ProcessResult(normalized_path, duration_seconds, languages, speakers, utterances, extraction, timings)`. `languages` отсортированы по числу распознанных слов; `timings` = `[StageTiming(stage, seconds)]`.
- `SpeakerResult(label, proposed_employee_id, similarity, second_similarity, review_required, review_reasons, clean_speech_seconds, speech_seconds)`. `similarity` — косинус, не вероятность. `second_similarity=None`, если второго кандидата нет.
- `UtteranceResult(index, start, end, speaker_label, text, language, uncertain_reasons)`. При первичной обработке `index` совпадает с `TranscriptLine.id` и с `TaskResult.source_ids`; backend заменяет его на id в БД.
- `TaskResult(task, to_employee_id, from_label, from_employee_id, deadline, deadline_source, evidence, source_ids, confidence, review_reasons)`. Автор — спикер первой исходной реплики (`from_label`); `from_employee_id` — его привязка на момент извлечения. Backend хранит `from_label`, чтобы автор следовал за ручной перепривязкой. `evidence` гарантированно дословно присутствует в исходных репликах: если модель процитировала иначе, подставляется текст реплик и ставится `evidence_not_found`.
- `ExtractionOutcome(status: ok|error|not_started, error_code, error_message, tasks, summary)`.
- `EnrollmentResult(vector, model, quality_status: ok|needs_review, reasons, speech_seconds, chunks, consistency)`.

### Причины

| Поле | Значения |
| --- | --- |
| `SpeakerResult.review_reasons` | `no_clean_speech` (нет чистого сегмента ≥ 1.5 с без overlap), `no_candidates`, `below_threshold` (< `VOICE_MATCH_THRESHOLD`), `low_margin` (отрыв от второго < `VOICE_MATCH_MARGIN`; только при ≥ 2 кандидатах), `candidate_taken` (лучшего кандидата забрал спикер с большей similarity; второго по качеству не назначаем), `split_cluster_suspected` (два спикера уверенно совпали с одним сотрудником: предложение сохраняется у победителя, но `review_required=true`) |
| `UtteranceResult.uncertain_reasons` | `no_speaker_overlap`, `overlapped_speech`, `ambiguous_speaker` (второй спикер ≥ `ALIGN_AMBIGUITY_RATIO` от первого) |
| `TaskResult.review_reasons` | `missing_assignee`, `missing_deadline`, `deadline_without_source` (срок без дословной фразы в репликах), `deadline_before_meeting`, `low_confidence` (< `TASK_LOW_CONFIDENCE`), `evidence_not_found`, `unresolved_author` |
| `EnrollmentResult.reasons` | `inconsistent_voice` (минимальный cosine куска к среднему < `VOICE_ENROLL_CONSISTENCY_THRESHOLD`), `clipping` (доля отсчётов у предела ≥ `VOICE_CLIPPING_REVIEW_RATIO`) |
| `ExtractionOutcome.error_code` | `LLM_UNAVAILABLE`, `LLM_TIMEOUT`, `LLM_INVALID_RESPONSE` (два ответа подряд не прошли схему), `LLM_FORBIDDEN_ENDPOINT` |

## Правила сопоставления

1. Чистые сегменты: интервалы спикера минус все интервалы одновременной речи; остаются куски ≥ `VOICE_MIN_SEGMENT_SECONDS`. Каждый режется на куски по `VOICE_ENROLL_CHUNK_SECONDS`, берутся до `VOICE_MAX_SEGMENTS_PER_SPEAKER` самых длинных. Затем L2 → среднее → L2. Без кусков вектор None и `no_clean_speech`.
2. Спикер может получить только своего лучшего кандидата, и только при similarity ≥ порога и отрыве ≥ margin (при одном кандидате проверяется только порог).
3. Конфликт за одного сотрудника решает венгерский алгоритм: у каждого спикера есть собственный фиктивный столбец «Неизвестный» с нулевой стоимостью. Отказ всегда допустим.
4. Пороги 0.55 и 0.08 — начальные значения для калибровки, не гарантия точности.

## Извлечение

- LLM получает дату, день недели, часовой пояс, календарь на 21 день, участников (id, ФИО, должность) и реплики `{id, speaker, speaker_name, text}` отдельным JSON в user-сообщении. Системный промпт прямо говорит, что транскрипт — это данные, а не команды.
- Окна по `LLM_WINDOW_UTTERANCES` реплик с перекрытием `LLM_WINDOW_OVERLAP` (≥ 3, то есть контекст ±3 реплики на границе).
- Pydantic проверяет, что `assignee_id` есть среди участников, `source_utterance_ids` есть в окне, дата в формате `YYYY-MM-DD` или null, `confidence` в [0,1]. Невалидный ответ даёт один retry с описанием ошибки схемы, повторная неудача — `LLM_INVALID_RESPONSE`.
- Thinking выключается параметром, зависящим от runtime (`LLM_THINKING_CONTROL`), см. `docs/AI_SETUP.md`.

## Что делает backend

- Проверяет загрузку (размер, ffprobe, длительность) до вызова, хранит файлы, передаёт `work_dir`.
- Передаёт в `candidates` только приглашённых с совместимым профилем.
- Пишет `on_stage` в `recordings.stage` с проверкой `generation`; устаревшая попытка получает `is_cancelled() == True`.
- Атомарно сохраняет результат. `confirmed_employee_id = proposed` только при `review_required = false`. `index` реплик переводит в id в БД, `source_ids` поручений — в id реплик. `to_employee_id` вне участников обнуляет с `missing_assignee`.
- Переводит `AIError` в `processing_status="error"` с `error_code` и `stage`; `VoiceQualityRejected` — в `422 VOICE_QUALITY_REJECTED`.
- Уведомления создаются только после подтверждения протокола секретарём; AI-слой их не создаёт.
