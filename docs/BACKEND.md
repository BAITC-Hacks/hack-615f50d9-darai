# DARAI backend

FastAPI + PostgreSQL/pgvector. CRM, аутентификация, роли, встречи, записи и фоновые задания, проверка протокола, поручения, уведомления, экспорт. AI-пайплайн (ASR, диаризация, голос, LLM) реализует AI-агент: см. `docs/AI_CONTRACT.md`, `docs/AI_SETUP.md`, `docs/AI_HANDOFF.md`. HTTP-контракт — `docs/API_CONTRACT.md`, схема — `backend/openapi.json`.

## Структура

| Путь | Назначение |
| --- | --- |
| `app/main.py` | FastAPI, lifespan: миграции, первый admin, восстановление прерванных заданий, цикл напоминаний, опциональная предзагрузка моделей |
| `app/config.py` | Все env-настройки (`Settings`) |
| `app/db.py`, `app/models.py`, `migrations/` | SQLAlchemy 2, Alembic; `0001_initial` создаёт расширение `vector` и все таблицы |
| `app/auth.py` | scrypt-хеши, серверные сессии (в БД хранится sha256 токена), CSRF, роли, bootstrap admin |
| `app/access.py` | Серверные проверки доступа: видимость встреч, редактор/участник, недоступное → 404 |
| `app/routers/*` | HTTP-маршруты (без префикса `/api`) |
| `app/processing.py` | Фоновые задания: семафор `MAX_CONCURRENT_JOBS`, этапы, `generation`, атомарная запись результата, `INTERRUPTED` после рестарта |
| `app/ai_gateway.py` | Точка вызова `app.ai_pipeline`; без AI-логики и без запасного результата |
| `app/notifications.py` | Уведомления с `dedup_key` + `ON CONFLICT DO NOTHING`, напоминания |
| `app/export.py` | DOCX (python-docx, шрифт DejaVu Sans) и PDF (LibreOffice headless, изолированный профиль, таймаут) |
| `app/uploads.py`, `app/audio.py` | Лимит размера, серверные имена файлов, ffprobe/длительность, нормализация FFmpeg |
| `tests/api/` | Тесты backend на реальной PostgreSQL + pgvector |
| `scripts/export_openapi.py` | Экспорт `openapi.json` |

AI-файлы (`asr.py`, `diarize.py`, `voice.py`, `align.py`, `extract.py`, `ai_pipeline.py`, `ai_types.py`, `ml.py`, `llm.py`, `requirements-ml.txt`, `tests/ai/`) принадлежат AI-агенту.

## Учётные записи и собственный профиль

1. Admin: `POST /employees` → `POST /users {employee_id, login[, role=employee]}`. Ответ содержит `temporary_password` (≈96 бит, `secrets.token_urlsafe`) один раз; в БД только scrypt-хеш, `must_change_password=true`. Передать сотруднику вручную по корпоративному каналу — backend ничего не отправляет.
2. Сотрудник входит временным паролем. Пока `must_change_password=true`, `get_current_user` пропускает только `GET /auth/me`, `POST /auth/change-password`, `POST /auth/logout`; всё остальное — `403 PASSWORD_CHANGE_REQUIRED` (проверка на сервере, обход UI не помогает).
3. `POST /auth/change-password {current_password, new_password}` (≥10 символов, ≠ текущему и логину): отзываются **все** сессии пользователя, в ответе — новая сессия и новый `csrf_token`.
4. `GET /employees/me` — собственная карточка (ФИО, должность, департамент, логин, статус голоса). `POST|DELETE /employees/me/voice` — регистрация/замена/удаление своего голоса через существующий enrollment; чужой id → 403; неудачная повторная регистрация профиль не меняет. Сохранённый профиль сразу доступен сопоставлению (кандидаты берутся из `voice_profiles` при каждой обработке).
5. Admin: `POST /users/{id}/reset-password` — новый временный пароль, все сессии отозваны; прочитать существующий пароль нельзя. `PATCH /users/{id} {active:false}` — сессии отозваны, вход запрещён. Поле `password` в `/users` больше не принимается.

Миграция `0002` добавляет `users.must_change_password` (server default `false`, существующие аккаунты не затрагиваются) и `users.password_changed_at`. Все ответы backend — `Cache-Control: no-store`. Пароли и токены не логируются.

## Жизненный цикл записи

```text
POST /meetings/{id}/recordings
  → ffprobe/лимиты синхронно (413/422) → recordings: processing/queued, generation=1 → 202
  → BackgroundTasks → run_recording_job (поток; семафор MAX_CONCURRENT_JOBS)
      ai_pipeline.process_recording(on_stage → recordings.stage, is_cancelled → generation устарела)
      → одна транзакция: спикеры, реплики (index→id), черновые поручения, саммари,
        extraction ok|error, done, draft_revision+1, уведомление protocol_ready|processing_failed
  ошибка AIError → processing_status=error, error_code, stage (сообщение без содержимого)
  рестарт → все processing → error INTERRUPTED (+уведомление), повтор кнопкой retry
retry: error → полный повтор; done + extraction error → только извлечение по сохранённому транскрипту
```

Состояния разделены: `recordings.processing_status` (обработка), `meetings.approval_status` (утверждение), `tasks.execution_status` (исполнение). Черновые поручения исполнителям не видны и уведомлений не создают; `POST /meetings/{id}/confirm` в одной транзакции с блокировкой встречи переводит поручения в `confirmed` и создаёт `task_assigned`. Спикер без уверенного совпадения остаётся «Неизвестным» (`confirmed_employee_id = null`), backend не назначает его сам.

## Запуск (Docker Compose)

Подготовка (с интернетом, один раз) → запуск (без интернета).

1. Проверить оборудование и диск **до** загрузки весов: `docker info`, свободное место (`df -h`), RAM. Ориентир: образ backend ~3.1 ГБ (CPU, arm64), образ ollama ~2–4 ГБ, faster-whisper large-v3 ~3 ГБ, pyannote ~0.1 ГБ, LLM 5–20 ГБ.
2. `cp .env.example .env`; задать `ADMIN_PASSWORD`, `LLM_MODEL`, при необходимости `POSTGRES_PASSWORD`.
3. Образы: `docker compose build && docker compose pull db ollama`.
4. Веса моделей в `./models` (read-only в контейнере как `/models`) — по `docs/AI_SETUP.md` (AI-агент; pyannote требует `HF_TOKEN` и принятия условий только на этом шаге). Ожидаемая раскладка по умолчанию:

   ```text
   models/faster-whisper-large-v3/model.bin …
   models/pyannote/speaker-diarization-3.1/config.yaml
   models/pyannote/segmentation-3.0/pytorch_model.bin
   models/pyannote/wespeaker-voxceleb-resnet34-LM/pytorch_model.bin
   models/ollama/…            (хранилище Ollama)
   ```

5. LLM для контейнера `ollama` (сеть `internal` без выхода наружу, поэтому загрузка — отдельным временным контейнером):

   ```bash
   docker run -d --name darai-ollama-prep -v "$PWD/models/ollama:/root/.ollama" ollama/ollama:0.33.2
   docker exec darai-ollama-prep ollama pull "$LLM_MODEL"
   docker rm -f darai-ollama-prep
   ```

6. Запуск: `docker compose up`. UI: `http://127.0.0.1:8080` (`FRONTEND_PORT`). Первый вход — `ADMIN_LOGIN`/`ADMIN_PASSWORD`. Проверка моделей: `GET /api/system/status` под admin.

Сети: `db`, `backend`, `ollama` — только в `internal` (`internal: true`, без egress); наружу опубликован только `frontend` (по умолчанию на 127.0.0.1). БД и LLM не публикуются.

LLM на хосте (например, Ollama с Metal на macOS — в Docker Desktop контейнерам GPU недоступен):

```bash
docker compose -f docker-compose.yml -f docker-compose.host-llm.yml up
```

Этот вариант даёт backend маршрут к хосту, т.е. Docker уже не изолирует его egress — ограничьте firewall хоста. Backend в любом случае отказывается от LLM на не-private адресах и от моделей Ollama `:cloud` (`LLM_FORBIDDEN_ENDPOINT`).

## Локальная разработка

```bash
cd backend
uv venv -p 3.11 .venv
uv pip install -p .venv -r requirements.txt -r requirements-dev.txt   # + -r requirements-ml.txt для AI
docker run -d --name darai-pg -e POSTGRES_USER=darai -e POSTGRES_PASSWORD=darai -e POSTGRES_DB=darai \
  -p 127.0.0.1:55433:5432 pgvector/pgvector:0.8.0-pg16
export DATABASE_URL=postgresql+psycopg://darai:darai@127.0.0.1:55433/darai DATA_DIR=./data \
       ADMIN_LOGIN=admin ADMIN_PASSWORD=admin-pass-1 LLM_BASE_URL=http://127.0.0.1:11434/v1 LLM_MODEL=qwen3:8b \
       MODELS_DIR=/path/to/models
.venv/bin/uvicorn app.main:app --port 8000 --workers 1
```

Frontend в dev: Vite проксирует `/api` → `http://127.0.0.1:8000` с удалением префикса. Миграции применяются при старте (`RUN_MIGRATIONS_ON_START=true`); вручную: `.venv/bin/alembic upgrade head`.

## Переменные окружения

Полный список с значениями по умолчанию — `.env.example` и `app/config.py`. Основные:

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `DATABASE_URL` | compose собирает из `POSTGRES_*` | PostgreSQL + pgvector |
| `DATA_DIR` | `/data` | Записи (`recordings/<meeting>/<recording>/`), временные файлы |
| `ADMIN_LOGIN`, `ADMIN_PASSWORD` | — | Первый admin, только при пустой таблице users |
| `SESSION_TTL_HOURS`, `COOKIE_SECURE` | 12, false | Сессии; `true` за TLS |
| `MAX_UPLOAD_MB`, `MAX_AUDIO_SECONDS`, `FFMPEG_TIMEOUT_SECONDS` | 500, 10800, 900 | Лимиты загрузки |
| `MAX_CONCURRENT_JOBS` | 1 | Одновременные тяжёлые задания |
| `PRELOAD_MODELS` | false | Загрузить модели при старте (иначе при первом задании) |
| `MODELS_DIR`, `ASR_*`, `DIARIZATION_CONFIG_PATH`, `EMBEDDING_MODEL_PATH`, `SEGMENTATION_MODEL_PATH`, `TORCH_DEVICE` | `/models/...` | Локальные модели |
| `VOICE_MATCH_THRESHOLD`, `VOICE_MATCH_MARGIN`, `VOICE_MIN_SEGMENT_SECONDS` | 0.55, 0.08, 1.5 | Идентификация (калибровать) |
| `VOICE_MODEL_ID`, `VOICE_MODEL_REVISION`, `VOICE_EMBEDDING_DIM` | wespeaker / local / 256 | Совместимость профилей: иной профиль → `incompatible` |
| `LLM_BASE_URL`, `LLM_MODEL` | `http://ollama:11434/v1`, обязателен (рекомендован `qwen3:8b`, см. `docs/AI_SETUP.md`) | Только локальный OpenAI-совместимый endpoint |
| `ASR_MULTILINGUAL`, `ASR_REFINE_LANGUAGES`, `ASR_ALLOWED_LANGUAGES`, `ASR_LANGUAGE_MIN_PROB`, `ASR_VAD_FILTER`, `DIARIZATION_MAX_SPEAKERS`, `VOICE_CLIPPING_*` | см. `.env.example` | Читаются AI-пайплайном |
| `REMINDER_INTERVAL_SECONDS`, `REMINDER_LEAD_HOURS` | 60, 24 | Напоминания |
| `SOFFICE_PATH`, `SOFFICE_TIMEOUT_SECONDS`, `EXPORT_FONT` | soffice, 120, DejaVu Sans | Экспорт |

## Тесты

```bash
cd backend
docker run -d --name darai-test-pg -e POSTGRES_USER=darai -e POSTGRES_PASSWORD=darai -e POSTGRES_DB=darai \
  -p 127.0.0.1:55433:5432 pgvector/pgvector:0.8.0-pg16
TEST_DATABASE_URL=postgresql+psycopg://darai:darai@127.0.0.1:55433/darai .venv/bin/python -m pytest tests/api
.venv/bin/python scripts/export_openapi.py   # после изменения API
```

Тесты пересоздают базу `darai_test`. AI-пайплайн в них заменён явно названным `TestAIAdapter` (`tests/api/fake_ai.py`, подключается только через `ai_gateway.set_test_adapter` в тестах): они проверяют CRM/БД/HTTP, **не** качество моделей. Тесты AI — `tests/ai/` (AI-агент).

Покрыто: вход/выход/CSRF/деактивация; выдача аккаунта с временным паролем (хранится только хеш), вход временным паролем, запрет обхода смены пароля прямыми запросами, смена пароля (неверный текущий, слабый новый, CSRF), отзыв других сессий и выдача новой текущей, сброс админом, запрет эскалации роли, деактивация со старой сессией, собственная карточка и собственный голос (регистрация, замена, неудачная замена сохраняет профиль, удаление, запрет чужого), миграция не выставляет флаг существующим; роли; чужая встреча, запись, аудио, экспорт, уведомление и голосовой профиль по подставленному ID; черновики скрыты от участников; регистрация голоса (согласие, отклонение качества без потери прежнего профиля, невалидный файл); создание встречи JSON и multipart, приглашения без дублей; сохранение результата pipeline; неизвестный спикер остаётся неизвестным; в кандидаты попадают только приглашённые с совместимым профилем; ошибка модели сохраняется как ошибка, а не результат; ошибка LLM отличается от «поручений нет», повтор выполняет только извлечение; устаревшая generation отбрасывается; рестарт → `INTERRUPTED`, поздний результат не перезаписывает ошибку; ручные правки спикеров/поручений/саммари сохраняются, автор следует за привязкой спикера; подтверждение атомарно, требует отметки для неполных данных, проверяет `draft_revision`, повтор не дублирует уведомления; исполнитель видит только подтверждённые задачи и меняет только статус исполнения; напоминания «скоро срок»/«просрочено» с управляемыми часами без дублей, выполненные и черновые исключены; экспорт DOCX содержит правки и казахские символы; PDF без LibreOffice — явная `503`; актуальность `openapi.json`.

## Результаты проверки (2026-09-23)

Оборудование: Apple M4 Pro, 48 GB RAM, Docker Desktop (linux/arm64, только CPU), ~15 GB свободного диска на момент проверки.

| Проверка | Результат |
| --- | --- |
| `tests/api` локально (Python 3.11, PostgreSQL 16 + pgvector 0.8.0 в Docker) | 28 passed, 1 skipped (PDF: нет LibreOffice на хосте) |
| `tests/api` внутри образа `darai-backend:local` | 29 passed, включая PDF через LibreOffice 7.4.7 |
| Импорт torch 2.5.1 / pyannote.audio 3.3.2 / faster-whisper 1.1.1 в образе | OK (образ 3.1 GB) |
| PDF с RU/KZ (Ә Ғ Қ Ң Ө Ұ Ү Һ І) из образа | Открывается, символы читаются (DejaVu Sans), A4 |
| `docker compose up` (db, backend, frontend; LLM на хосте через override) | `/api/health` ok через nginx; вход, сотрудник, встреча, загрузка через `/api`; экспорт PDF 200 |
| Настоящий `app.ai_pipeline` без весов | Запись → `error`, этап `transcribing`, `MODEL_UNAVAILABLE` с путём к отсутствующему `model.bin`; регистрация голоса → `503 MODEL_UNAVAILABLE`, временный образец удалён. Фиктивного результата нет |
| `GET /system/status` с хостовой Ollama `llama3.1:8b` | `endpoint_allowed: true`, `reachable: true` |
| Egress backend в compose по умолчанию | Заблокирован (`URLError` при запросе в интернет); с `docker-compose.host-llm.yml` — открыт, как и задокументировано |

**Не проверено backend-агентом:** полный путь на настоящих весах (ASR/диаризация/эмбеддинги) и качество RU/KZ/mixed — зона AI-агента, см. `docs/AI_SETUP.md`/`docs/AI_HANDOFF.md`; контейнер `ollama` в compose (образ не загружался из-за места на диске); загрузка файлов ~1 GB через nginx; работа на GPU.

## Ограничения MVP

- BackgroundTasks — не надёжная очередь: прерванное задание получает `INTERRUPTED`, повтор вручную. Один процесс uvicorn (семафор и планировщик напоминаний внутри процесса).
- Одна активная запись на встречу; новая загрузка заменяет черновые результаты; после подтверждения протокол неизменяем (нет версионирования правок).
- Нет TLS, шифрования хранилища, аудита, политики удаления, rate limit на вход — требуется для эксплуатации.
- `GET /tasks` фильтр `overdue` и пагинация выполняются в Python по выборке пользователя.
- Исходные загруженные записи хранятся в `DATA_DIR` до замены; автоматического удаления нет.
