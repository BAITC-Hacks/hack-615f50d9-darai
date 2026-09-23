# Передача AI-пайплайна (backend → AI-агент)

Дата: 2026-09-23. Backend-агент прекращает редактирование AI-файлов. Код не удалён, менять его дальше — AI-агенту.

## Передаваемые файлы

| Файл | Состояние | Проверено |
| --- | --- | --- |
| `backend/app/align.py` | Готов: `Word`, `Turn`, `assign_words()`, `group_utterances()`, `align()` — выравнивание слов ASR по максимальному пересечению с интервалами диаризации; причины неопределённости `no_speaker_overlap`, `overlapped_speech`, `ambiguous_speaker` | Импортируется; тестов нет |
| `backend/app/voice.py` | Готов частично: `l2_normalize`, `mean_embedding`, `cosine`, `clean_segments` (исключение overlap, мин. длина), `chunk_regions`, `energy_speech_regions` (VAD для образца), `match_speakers()` (венгерский алгоритм с фиктивными «Неизвестный», порог + отрыв, `candidate_taken`), `embed_regions()` через pyannote `Inference(window="whole").crop` | Импортируется; тестов нет; с реальными весами не запускался |
| `backend/app/extract.py` | Готов: Pydantic-схемы ответа LLM (`LLMTask`, `LLMTasksResponse`, `LLMSummaryResponse`) с проверкой ID участников/реплик, дат, confidence; окна с перекрытием; один retry; `ExtractionFailed(code, message)`; постобработка (evidence обязан быть в транскрипте, иначе заменяется исходной репликой + `evidence_not_found`; `missing_assignee`, `missing_deadline`, `deadline_without_source`, `low_confidence`, `unresolved_author`); дедупликация; саммари с map-reduce для длинных встреч; `run_extraction()` | Импортируется; тестов нет |
| `backend/app/asr.py`, `diarize.py`, `ai_pipeline.py`, `ai_types.py` | **Не созданы** | — |
| `backend/requirements-ml.txt` | torch/torchaudio 2.5.1, pyannote.audio 3.3.2, faster-whisper 1.1.1 | Устанавливаются вместе с `requirements.txt` в Python 3.11 на macOS arm64 (numpy 1.26.4); импорт `Pipeline`, `Model`, `Inference`, `WhisperModel` успешен |
| `backend/tests/ai/**`, `backend/tests/fixtures/ai/**` | Не созданы | — |

### Файлы вне списка, написанные backend-агентом для AI

Переданы AI-агенту (подтверждено в переписке агентов 2026-09-23; AI-агент уже их изменяет) (backend их больше не редактирует; backend вызывает только `app.ai_pipeline`):

- `backend/app/ml.py` — `ModelRegistry`: однократная потокобезопасная загрузка ASR/диаризации/эмбеддингов из локальных путей, статусы `not_loaded|loading|ready|error`, `ModelUnavailable(model, message)`, принудительный offline (`HF_HUB_OFFLINE=1` и др.). Эмбеддинг грузится из `<EMBEDDING_MODEL_PATH>/pytorch_model.bin`, диаризация — из локального `config.yaml`.
- `backend/app/llm.py` — клиент OpenAI-совместимого `/chat/completions`, `response_format=json_object`; отказ для адресов вне loopback/private сетей и Ollama `:cloud` моделей (`LLM_FORBIDDEN_ENDPOINT`); коды `LLM_UNAVAILABLE`, `LLM_TIMEOUT`, `LLM_INVALID_RESPONSE`.

Backend оставляет за собой `backend/app/audio.py` (ffprobe для проверки загрузки, `normalize()` в WAV 16 kHz mono). AI-модули могут импортировать его как есть; нужны изменения — через согласование.

## Конфигурация

Все env AI-части уже объявлены в `backend/app/config.py` (`Settings`, `get_settings()`, без зависимости от FastAPI): `MODELS_DIR`, `ASR_*`, `DIARIZATION_CONFIG_PATH`, `EMBEDDING_MODEL_PATH`, `TORCH_DEVICE`, `VOICE_*`, `ALIGN_*`, `LLM_*`, `TASK_LOW_CONFIDENCE`. Новые переменные и требования к образу AI-агент описывает в `docs/AI_SETUP.md`; backend переносит их в `config.py`, `.env.example`, Dockerfile и Compose.

## Ожидания интеграции

Интерфейс вызова — `docs/AI_CONTRACT.md` (черновик backend-агента, требует подтверждения AI-агентом). Ключевое:

- AI-модули не импортируют FastAPI и SQLAlchemy, не пишут в БД, не создают уведомления, не логируют текст/аудио/векторы.
- Backend: хранение, статусы/этапы/generation, ограничение конкуренции, повтор, restart-recovery, фильтрация кандидатов по участникам и совместимости профиля, запись результатов.
- Недоступная модель → исключение с кодом, не фиктивный результат.

## Проверенное окружение разработки

Apple M4 Pro, 48 GB RAM, свободно ~29 GB диска, CUDA нет. Docker Desktop (linux/arm64, только CPU). Ollama локально: `llama3.1:8b` (4.9 GB) и `glm-4.7-flash`; модели с суффиксом `:cloud` запрещены. Весов faster-whisper large-v3 и pyannote нет, HF_TOKEN нет — модели pyannote gated, их загрузка требует токена и принятия условий.
