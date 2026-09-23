# DARAI — контракт HTTP API (backend ↔ frontend)

Версия контракта: **1.3.0** (зафиксирована 2026-09-23; 1.0.1 — только дополнительные значения перечислений, см. «Журнал изменений»). Изменения — только по согласованию backend- и frontend-агентов, с обновлением этого файла и `backend/openapi.json`.

Источник требований: `docs/DEVELOPMENT_PLAN.md` §4. Машиночитаемая схема после реализации — `backend/openapi.json` (экспорт FastAPI). При расхождении этот документ описывает намерение, OpenAPI — фактические типы; расхождение считается ошибкой и исправляется.

## 0. Общие правила

### Префикс и транспорт

- Backend обслуживает маршруты **без** префикса: `GET /meetings`.
- Frontend (nginx в production, Vite proxy в разработке) обращается к `/api/...` и проксирует на `backend:8000`, **удаляя** `/api`. Пример: браузер → `GET /api/meetings` → backend `GET /meetings`.
- Тела запросов и ответов — JSON UTF-8, кроме multipart-загрузок, аудио и экспортируемых файлов.

### Идентификаторы и время

- `id` сотрудников, пользователей, встреч, записей, поручений, уведомлений — UUID строкой.
- `id` реплик (utterance) — целое число, уникально в рамках записи.
- Метки спикеров — строки `SPEAKER_00`, `SPEAKER_01`, … уникальны в рамках активной записи встречи.
- Моменты времени — ISO 8601 в UTC с `Z`: `"2026-09-23T09:00:00Z"`.
- Календарные даты (сроки) — `YYYY-MM-DD`.
- Часовой пояс — IANA-имя: `"Asia/Almaty"`.
- Неизвестные значения — JSON `null`, никогда строка `"null"`.

### Аутентификация и CSRF

- `POST /auth/login` устанавливает две cookie:
  - `darai_session` — HttpOnly, `SameSite=Lax`, `Path=/`, `Secure` при `COOKIE_SECURE=true`. Серверная сессия, срок `SESSION_TTL_HOURS` (по умолчанию 12).
  - `darai_csrf` — **не** HttpOnly, то же значение возвращается в теле как `csrf_token`.
- Любой изменяющий запрос (`POST`, `PATCH`, `PUT`, `DELETE`), кроме `POST /auth/login`, обязан передать заголовок `X-CSRF-Token: <csrf_token>`. Нет/неверный → `403 CSRF_FAILED`.
- Нет сессии/истекла → `401 UNAUTHENTICATED`. Frontend при 401 переходит на экран входа.
- `fetch` должен отправлять cookie: `credentials: "include"` (или same-origin по умолчанию через прокси).

### Роли

| Роль | Возможности |
| --- | --- |
| `admin` | Всё: сотрудники, учётные записи, любые встречи, голосовые профили любого сотрудника, статус системы |
| `secretary` | Создаёт встречи; редактирует и подтверждает встречи, где он организатор или секретарь; видит справочник сотрудников |
| `employee` | Видит встречи, где участвует; подтверждённые протоколы; свои поручения и уведомления; регистрирует свой голос |

Понятия для встречи:

- **Редактор встречи** (`can_edit`) — admin, организатор или назначенный секретарь встречи.
- **Участник** — пользователь, чей `employee_id` входит в участники встречи.
- Встреча, недоступная пользователю, возвращает `404 NOT_FOUND` (не 403), чтобы не раскрывать существование по подставленному ID.
- Черновое содержимое (транскрипт, спикеры, черновые поручения, саммари) видят только редакторы. Участники видят содержимое после подтверждения протокола (`content_visible`).

### Формат ошибок

Все ошибки, включая ошибки валидации:

```json
{
  "error": {
    "code": "DRAFT_REVISION_MISMATCH",
    "message": "Протокол изменился после загрузки страницы",
    "details": {"current_draft_revision": 7}
  }
}
```

`message` — человекочитаемый текст на русском, пригоден для показа. `details` — объект или `null`. Frontend ветвится только по `code`.

| HTTP | code | Когда |
| --- | --- | --- |
| 400 | `BAD_REQUEST` | Некорректный запрос, не подпадающий под другие коды |
| 401 | `UNAUTHENTICATED` | Нет сессии/истекла |
| 401 | `INVALID_CREDENTIALS` | Неверный логин/пароль или неактивная учётная запись |
| 403 | `CSRF_FAILED` | Нет/неверный `X-CSRF-Token` |
| 403 | `FORBIDDEN` | Сущность видна, но действие запрещено ролью |
| 403 | `PASSWORD_CHANGE_REQUIRED` | У учётной записи `must_change_password = true`; разрешены только `GET /auth/me`, `POST /auth/change-password`, `POST /auth/logout` |
| 422 | `INVALID_CURRENT_PASSWORD` | Неверный текущий пароль при смене |
| 422 | `WEAK_PASSWORD` | Новый пароль короче 10 символов, совпадает с текущим или с логином |
| 404 | `NOT_FOUND` | Нет сущности или нет доступа к ней |
| 409 | `CONFLICT` и специфичные коды ниже | Конфликт состояния |
| 413 | `FILE_TOO_LARGE` | Файл больше `MAX_UPLOAD_MB` |
| 422 | `VALIDATION_ERROR` | Ошибка схемы; `details.fields = [{"loc": ["body","title"], "msg": "..."}]` |
| 422 | `AUDIO_INVALID` | Файл не распознан FFmpeg как аудио/видео или нет аудиодорожки |
| 422 | `AUDIO_TOO_LONG` | Длительность больше `MAX_AUDIO_SECONDS` |
| 422 | `VOICE_QUALITY_REJECTED` | Образец голоса не прошёл проверку качества |
| 503 | `MODEL_UNAVAILABLE` | Локальная модель не найдена/не загрузилась; `details.model` |
| 503 | `EXPORT_UNAVAILABLE` | Нет LibreOffice для PDF |
| 500 | `INTERNAL_ERROR` | Непредвиденная ошибка; содержимое не раскрывается |

Специфичные коды 409: `SELF_DELETE_FORBIDDEN`, `LAST_ADMIN`, `EMPLOYEE_ARCHIVED`, `LOGIN_TAKEN`, `EMPLOYEE_HAS_ACCOUNT`, `MEETING_CONFIRMED`, `RECORDING_PROCESSING`, `RECORDING_NOT_READY`, `RETRY_NOT_ALLOWED`, `DRAFT_REVISION_MISMATCH`, `ALREADY_CONFIRMED`, `CONFIRMATION_REQUIRES_ACKNOWLEDGEMENT`, `PARTICIPANT_IN_USE`, `TASK_NOT_CONFIRMED`.

### Пагинация

Списки принимают `?limit=50&offset=0` (`limit` 1–200, по умолчанию 50) и отвечают:

```json
{"items": [], "total": 0, "limit": 50, "offset": 0}
```

### Polling

- Статус обработки: `GET /meetings/{id}/recordings/{recording_id}` каждые 3 с, пока `processing_status = "processing"`.
- Уведомления: `GET /notifications?unread_only=true&limit=1` (поле `unread_count`) каждые 30 с.

---

## 1. Auth

### `POST /auth/login`

```json
{"login": "secretary", "password": "secret-pass-1"}
```

`200`:

```json
{
  "user": {
    "id": "0f7c1f7e-1d8b-4a53-9a2e-5d0a9a1d7b10",
    "login": "secretary",
    "role": "secretary",
    "employee": {
      "id": "7b2d8a1e-9d0c-4b7e-8f55-1f3c2a9e6d41",
      "fio": "Иванова Анна Сергеевна",
      "position": "Секретарь",
      "department": "Аппарат"
    }
  },
  "csrf_token": "p1Yk...43 символа"
}
```

Ошибки: `401 INVALID_CREDENTIALS`, `422 VALIDATION_ERROR`.

### `POST /auth/logout`

`204`, cookie удаляются. Без сессии — тоже `204`.

### `GET /auth/me`

`200` — объект `user` как выше плюс `"csrf_token"` (для восстановления после перезагрузки страницы):

```json
{"id": "...", "login": "secretary", "role": "secretary", "employee": {"id": "...", "fio": "...", "position": "...", "department": "..."}, "csrf_token": "..."}
```

`employee` может быть `null` только у технического администратора.

Во всех объектах пользователя (`user` в ответе входа, `GET /auth/me`) есть поле `"must_change_password": true|false`.

### Обязательная смена временного пароля

Если `must_change_password = true` (аккаунт только что выдан или пароль сброшен администратором), backend для **любого** другого маршрута отвечает `403 PASSWORD_CHANGE_REQUIRED`. Разрешены только `GET /auth/me`, `POST /auth/change-password`, `POST /auth/logout`. Вход (`POST /auth/login`) с временным паролем успешен и возвращает `must_change_password: true` — frontend сразу показывает форму смены пароля.

### `POST /auth/change-password`

Только для текущего пользователя (чужой `id` передать нельзя). Требует `X-CSRF-Token`.

```json
{"current_password": "временный-или-текущий", "new_password": "не короче 10 символов"}
```

`200` — тот же формат, что `POST /auth/login` (`{"user": {..., "must_change_password": false}, "csrf_token": "..."}`).

Поведение сессий: **все** сессии пользователя, включая текущую, отзываются; в этом же ответе выдаётся новая сессия (новые cookie `darai_session`/`darai_csrf`) и новый `csrf_token`, который frontend обязан сохранить. Другие устройства получат `401`.

Ошибки: `422 INVALID_CURRENT_PASSWORD`, `422 WEAK_PASSWORD` (короче 10 символов, равен текущему или логину), `422 VALIDATION_ERROR`.

### Кеширование

Все ответы backend содержат `Cache-Control: no-store` (в т.ч. с временным паролем и CSRF-токеном).

---

## 2. Учётные записи (только admin)

User:

```json
{
  "id": "0f7c1f7e-...",
  "login": "d.akhmetova",
  "role": "employee",
  "active": true,
  "must_change_password": true,
  "employee_id": "5c1e...",
  "employee_fio": "Ахметова Дана Ерлановна",
  "created_at": "2026-09-23T08:00:00Z"
}
```

| Метод и путь | Тело | Ответ |
| --- | --- | --- |
| `GET /users?limit&offset` | — | `200` страница User |
| `POST /users` | `{"employee_id": "uuid", "login": "d.akhmetova", "role": "employee"}`; `role` необязательна, по умолчанию `employee` | `201` UserWithTemporaryPassword; `409 LOGIN_TAKEN`, `409 EMPLOYEE_HAS_ACCOUNT`, `404` сотрудника нет |
| `POST /users/{id}/reset-password` | — | `200` UserWithTemporaryPassword. Все сессии пользователя отзываются, `must_change_password = true` |
| `PATCH /users/{id}` | любые из `{"role", "active"}` | `200` User. Деактивация отзывает все сессии; ранее выданные cookie дают `401`. Администратор не может снять с себя роль admin или деактивировать себя (`409 CONFLICT`) |

UserWithTemporaryPassword = User + `"temporary_password": "..."`:

```json
{"id": "...", "login": "d.akhmetova", "role": "employee", "active": true, "must_change_password": true,
 "employee_id": "...", "employee_fio": "...", "created_at": "...", "temporary_password": "Xq7-...16+ символов"}
```

- Временный пароль генерируется backend криптографически случайно и возвращается **только** в этом ответе. В БД хранится лишь scrypt-хеш; повторно получить пароль нельзя — только сбросить. Передаётся сотруднику вручную по корпоративному каналу; backend ничего не отправляет.
- Поле `password` в `POST /users`/`PATCH /users/{id}` больше не принимается (`422 VALIDATION_ERROR`): администратор не задаёт и не видит постоянных паролей.
- `login`: 3–64 символа `[A-Za-z0-9._-]`, уникален.

---

## 3. Сотрудники и голосовые профили

Employee:

```json
{
  "id": "5c1e0c0a-...",
  "fio": "Ахметова Дана Ерлановна",
  "position": "Экономист",
  "department": "Финансовый департамент",
  "active": true,
  "has_account": true,
  "user_id": "0f7c...",
  "can_delete": false,
  "voice_profile": {
    "status": "ok",
    "quality_status": "ok",
    "speech_seconds": 24.6,
    "created_at": "2026-09-23T08:10:00Z",
    "consent_at": "2026-09-23T08:10:00Z"
  }
}
```

`voice_profile.status`:

- `none` — профиля нет (остальные поля `null`);
- `ok` — профиль совместим с текущей моделью;
- `needs_review` — сохранён, но проверка качества выявила риск (например, непостоянство голоса в образце); используется в сопоставлении, рекомендуется перезапись;
- `incompatible` — профиль создан другой моделью/ревизией/размерностью; в сопоставлении не участвует, нужна перерегистрация.

Голосовой вектор **никогда** не возвращается API.

| Метод и путь | Доступ | Тело/параметры | Ответ |
| --- | --- | --- | --- |
| `GET /employees` | любой вошедший | `q` (поиск по ФИО/должности/департаменту), `department`, `active` (по умолчанию `true`; `all` — все), `limit`, `offset` | `200` страница Employee |
| `GET /employees/{id}` | любой вошедший | — | `200` Employee |
| `POST /employees` | admin | `{"fio": "...", "position": "...", "department": "..."}` (каждое 1–255 символов) | `201` Employee |
| `PATCH /employees/{id}` | admin | любые из `{"fio", "position", "department", "active"}` | `200` Employee |
| `GET /employees/me` | любой вошедший с привязанным сотрудником | — | `200` MyProfile; `404`, если у учётной записи нет сотрудника |
| `DELETE /employees/{id}` | admin, secretary | — | `204` (в т.ч. повторно для уже архивированного); см. «Удаление сотрудника» |
| `POST /employees/{id}/voice` | admin или сам сотрудник | multipart, см. ниже | `201` VoiceEnrollment |
| `DELETE /employees/{id}/voice` | admin или сам сотрудник | — | `204`; `404` если профиля нет |

### Удаление сотрудника

`DELETE /employees/{employee_id}` — кнопка «Удалить», операция **архивирует** сотрудника. Cookie-сессия + `X-CSRF-Token`, тела нет.

- `204 No Content`; повторное удаление архивированного — тоже `204`. Неизвестный ID — `404 NOT_FOUND`.
- `403 FORBIDDEN`: роль `employee`; секретарь удаляет сотрудника, чья учётная запись имеет роль `secretary` или `admin`.
- `409 SELF_DELETE_FORBIDDEN`: удаление собственной карточки.
- `409 LAST_ADMIN`: удаление последнего активного администратора.
- Одной транзакцией: `active=false`, учётная запись деактивируется, все её сессии отзываются (старые cookie → `401`).
- Сотрудник исчезает из `GET /employees` (по умолчанию `active=true`; `total`/пагинация учитывают фильтр), его нельзя выбрать участником новой встречи или добавить в участники (`422 VALIDATION_ERROR`), выдать ему учётную запись или зарегистрировать голос (`409 EMPLOYEE_ARCHIVED`), реактивировать его учётную запись через `PATCH /users/{id}` (`409 EMPLOYEE_ARCHIVED`).
- Голосовой профиль сохраняется, но исключается из автоматической идентификации.
- Исторические встречи, участники, спикеры, протоколы и поручения не изменяются; поручения не закрываются и не переназначаются. Карточка по ID (`GET /employees/{id}`) и данные встреч продолжают показывать архивированного сотрудника (`active: false`).
- Физическое удаление аудио и биометрических данных не выполняется.

`can_delete` в Employee/MyProfile вычисляет сервер для текущего пользователя: admin — любого другого (кроме последнего активного admin); secretary — сотрудника без аккаунта или с ролью `employee`; employee — никогда; собственная карточка — никогда; уже архивированный — `false`. Сервер проверяет права независимо от поля.

MyProfile = Employee + `"login"`:

```json
{"id": "5c1e...", "fio": "Ахметова Дана Ерлановна", "position": "Экономист", "department": "Финансовый департамент",
 "active": true, "has_account": true, "user_id": "0f7c...", "login": "d.akhmetova",
 "voice_profile": {"status": "none", "quality_status": null, "speech_seconds": null, "created_at": null, "consent_at": null}}
```

Для собственного профиля вместо UUID можно использовать `me`: `POST /employees/me/voice`, `DELETE /employees/me/voice` (разрешается в `employee_id` текущего пользователя; подставить чужой id через `me` нельзя). Сотрудник может зарегистрировать, заменить и удалить только свой голос; чужой → `403 FORBIDDEN`. Неудачная повторная регистрация (`422 VOICE_QUALITY_REJECTED`, `503 MODEL_UNAVAILABLE`, `422 AUDIO_*`) не меняет существующий профиль. Отсутствие голоса не ограничивает вход, поручения и уведомления.

### `POST /employees/{id}/voice` — multipart/form-data

| Поле | Тип | Обязательно | Описание |
| --- | --- | --- | --- |
| `file` | файл | да | Любой формат, читаемый FFmpeg (wav, webm/opus из MediaRecorder, mp3, m4a). Ориентир — 20–30 с чистой речи одного человека |
| `consent` | `"true"` | да | Сотрудник уведомлён и согласен на обработку голосового профиля. Иначе `422 VALIDATION_ERROR` |

Операция синхронная (секунды). Исходный образец после вычисления вектора удаляется; повторная регистрация заменяет профиль.

`201`:

```json
{
  "status": "ok",
  "quality_status": "ok",
  "speech_seconds": 24.6,
  "reasons": [],
  "created_at": "2026-09-23T08:10:00Z"
}
```

`quality_status`: `ok` или `needs_review` (`reasons`: `["inconsistent_voice"]`, `["clipping"]`). Отклонённый образец не сохраняется, прежний профиль остаётся:

```json
{"error": {"code": "VOICE_QUALITY_REJECTED", "message": "Слишком мало чистой речи: 6.2 с, нужно не менее 15 с", "details": {"reasons": ["too_short"], "speech_seconds": 6.2, "required_seconds": 15.0}}}
```

`reasons` отклонения: `too_short`, `silence`, `clipping`. Модель недоступна → `503 MODEL_UNAVAILABLE` с `details.model = "embedding"`.

---

## 4. Встречи

### Объекты

UserRef:

```json
{"id": "0f7c...", "login": "secretary", "employee_id": "7b2d...", "fio": "Иванова Анна Сергеевна"}
```

Participant:

```json
{"employee_id": "5c1e...", "fio": "Ахметова Дана Ерлановна", "position": "Экономист", "department": "Финансовый департамент", "has_account": true, "voice_status": "ok"}
```

RecordingStatus:

```json
{
  "id": "a3d4...",
  "meeting_id": "c9e1...",
  "processing_status": "processing",
  "stage": "transcribing",
  "error_code": null,
  "error_message": null,
  "generation": 1,
  "original_filename": "meeting.m4a",
  "duration_seconds": 312.4,
  "languages": ["ru", "kk"],
  "extraction": {"status": "not_started", "error_code": null, "error_message": null},
  "audio_url": "/meetings/c9e1.../recordings/a3d4.../audio",
  "created_at": "2026-09-23T09:30:00Z",
  "updated_at": "2026-09-23T09:31:12Z"
}
```

- `processing_status`: `processing` | `done` | `error`.
- `stage`: `queued` → `normalizing` → `transcribing` → `diarizing` → `aligning` → `identifying` → `extracting` → `done`. При `error` — этап, на котором произошёл сбой.
- `error_code` обработки: `AUDIO_INVALID`, `AUDIO_EMPTY`, `AUDIO_TOO_LONG`, `FFMPEG_FAILED`, `MODEL_UNAVAILABLE`, `ASR_FAILED`, `DIARIZATION_FAILED`, `EMBEDDING_FAILED`, `INTERRUPTED` (backend перезапущен во время обработки), `INTERNAL_ERROR`.
- `extraction.status`: `not_started` | `ok` | `error`. `error_code` извлечения: `LLM_UNAVAILABLE`, `LLM_TIMEOUT`, `LLM_INVALID_RESPONSE` (дважды невалидный JSON/схема), `LLM_FORBIDDEN_ENDPOINT` (LLM_BASE_URL не локальный или облачная модель). При ошибке извлечения `processing_status = "done"`, транскрипт доступен, `tasks = []`, а UI обязан показать «извлечение не удалось», а не «поручений нет».
- `audio_url` — относительный путь backend (frontend добавляет `/api`). `null`, пока нормализованный WAV не готов.
- `languages` — языки, определённые ASR по сегментам (может быть пустым).

Speaker:

```json
{
  "label": "SPEAKER_00",
  "proposed_employee_id": "5c1e...",
  "confirmed_employee_id": "5c1e...",
  "manually_set": false,
  "similarity": 0.71,
  "second_similarity": 0.42,
  "review_required": false,
  "review_reasons": [],
  "clean_speech_seconds": 48.2,
  "utterance_count": 14
}
```

- `proposed_employee_id` — предложение сопоставления голоса (не меняется ручными правками).
- `confirmed_employee_id` — текущая привязка, используемая в транскрипте, поручениях и экспорте. Изначально равна предложению, если `review_required = false`, иначе `null`.
- `similarity` — cosine similarity лучшего кандидата, не вероятность. `null`, если сравнивать было не с чем.
- `review_reasons`: `no_clean_speech`, `no_candidates`, `below_threshold`, `low_margin`, `candidate_taken` (лучший кандидат назначен другому спикеру — возможно разбиение одного человека на кластеры), `split_cluster_suspected` (1.0.1: другой спикер тоже уверенно совпал с этим сотрудником; предложение сохранено, но требуется проверка). Frontend обязан показывать неизвестные значения как есть.

Utterance:

```json
{
  "id": 17,
  "speaker_label": "SPEAKER_00",
  "employee_id": "5c1e...",
  "start": 61.24,
  "end": 66.9,
  "text": "Дана, подготовьте отчёт по бюджету до пятницы.",
  "language": "ru",
  "uncertain": false,
  "uncertain_reasons": []
}
```

`start`/`end` — секунды от начала записи. `employee_id` = `confirmed_employee_id` спикера. `speaker_label` может быть `null`, если слово/реплика не пересеклись ни с одним интервалом диаризации. `uncertain_reasons`: `no_speaker_overlap`, `overlapped_speech`, `ambiguous_speaker`.

Task:

```json
{
  "id": "e5b0...",
  "meeting_id": "c9e1...",
  "meeting_title": "Планёрка финансового департамента",
  "from": "7b2d...",
  "from_speaker_label": "SPEAKER_00",
  "from_fio": "Иванова Анна Сергеевна",
  "to": "5c1e...",
  "to_fio": "Ахметова Дана Ерлановна",
  "task": "Подготовить отчёт по бюджету",
  "deadline": "2026-09-25",
  "deadline_source": "до пятницы",
  "deadline_at": "2026-09-25T18:59:59Z",
  "evidence": "Дана, подготовьте отчёт по бюджету до пятницы.",
  "source_utterance_ids": [17],
  "confidence": 0.82,
  "status": "draft",
  "execution_status": "in_progress",
  "completed_at": null,
  "overdue": false,
  "needs_review": false,
  "review_reasons": [],
  "origin": "llm",
  "created_at": "2026-09-23T09:35:00Z",
  "updated_at": "2026-09-23T09:35:00Z"
}
```

- `from` — автор: UUID сотрудника, если автор определён (ручная правка или привязка спикера), иначе метка `SPEAKER_XX`, иначе `null`. При смене привязки спикера `from` меняется автоматически, `to` — нет.
- `to` — UUID исполнителя (участник встречи) или `null`.
- `deadline` — дата или `null`; `deadline_source` — исходная формулировка срока или `null`. `deadline_at` — конец дня срока в часовом поясе встречи, в UTC; `null` без срока.
- `status`: `draft` | `confirmed` (утверждение протокола). `execution_status`: `in_progress` | `completed` (исполнение). `overdue` вычисляется, не хранится.
- `confidence` — оценка LLM в [0,1], не откалиброванная вероятность; для ручных задач `null`.
- `review_reasons`: `missing_assignee`, `missing_deadline`, `deadline_without_source`, `deadline_before_meeting` (1.0.1), `low_confidence`, `evidence_not_found`, `unresolved_author`. Frontend обязан показывать неизвестные значения как есть.
- `origin`: `llm` | `manual`.

MeetingListItem:

```json
{
  "id": "c9e1...",
  "title": "Планёрка финансового департамента",
  "starts_at": "2026-09-23T04:00:00Z",
  "timezone": "Asia/Almaty",
  "organizer": {"id": "...", "login": "secretary", "employee_id": "...", "fio": "..."},
  "secretary": {"id": "...", "login": "secretary", "employee_id": "...", "fio": "..."},
  "approval_status": "draft",
  "protocol_version": 0,
  "participant_count": 3,
  "recording": null,
  "can_edit": true
}
```

MeetingDetail = MeetingListItem плюс:

```json
{
  "agenda": "1. Бюджет\n2. Отчёты",
  "draft_revision": 4,
  "confirmed_at": null,
  "confirmed_by": null,
  "participants": [],
  "permissions": {"can_edit": true, "can_confirm": true, "can_export_draft": true},
  "content_visible": true,
  "speakers": [],
  "utterances": [],
  "summary": null,
  "summary_edited": false,
  "tasks": []
}
```

- `approval_status`: `draft` | `confirmed`. `protocol_version` — 0 до подтверждения, 1 после.
- `draft_revision` — счётчик изменений черновика (результаты обработки, спикеры, поручения, саммари, участники). Передаётся в `POST /meetings/{id}/confirm`.
- При `content_visible = false`: `speakers`, `utterances`, `tasks` — `[]`, `summary` — `null`.

### `GET /meetings`

Параметры: `q` (по названию), `approval_status`, `processing_status`, `date_from`, `date_to` (YYYY-MM-DD, по `starts_at` в UTC), `limit`, `offset`. Сортировка по `starts_at` убыв. Только доступные пользователю встречи. `200` страница MeetingListItem.

### `POST /meetings` — JSON (основной)

Доступ: admin, secretary. `Content-Type: application/json`:

```json
{
  "title": "Планёрка финансового департамента",
  "starts_at": "2026-09-23T09:00:00+05:00",
  "timezone": "Asia/Almaty",
  "agenda": "1. Бюджет",
  "participant_ids": ["5c1e...", "7b2d..."],
  "secretary_id": null
}
```

- `starts_at` без смещения трактуется в `timezone`.
- `secretary_id` — UUID **пользователя** с ролью secretary/admin; `null` → текущий пользователь. Организатор — текущий пользователь.
- `participant_ids` — активные сотрудники, 0–200, без повторов.

`201` MeetingDetail. Участники с учётными записями получают уведомление `meeting_invitation`.

### `POST /meetings` — multipart (быстрый вариант)

`Content-Type: multipart/form-data`, поля: `file` (запись), `title`, `starts_at`, `timezone`, `agenda` (необязательно), `participant_ids` (повторяемое поле; также принимается `participant_ids[]`), `secretary_id` (необязательно). `202` MeetingDetail с `recording` в статусе `processing`.

### `GET /meetings/{id}`

`200` MeetingDetail.

### `PATCH /meetings/{id}`

Редактор, только `draft`. Любые из `{"title", "agenda", "starts_at", "timezone", "participant_ids", "secretary_id"}`. `participant_ids` заменяет состав; новым участникам с аккаунтами отправляется приглашение. Удаление участника, привязанного к спикеру или являющегося исполнителем поручения → `409 PARTICIPANT_IN_USE`. `200` MeetingDetail. После подтверждения → `409 MEETING_CONFIRMED`.

---

## 5. Записи и обработка

### `POST /meetings/{id}/recordings`

Редактор, встреча `draft`. multipart: `file` (обязательно). `202` RecordingStatus.

- На MVP у встречи одна активная запись. Новая загрузка заменяет предыдущую (если та не `processing`) и удаляет её результаты: спикеров, реплики, саммари и **все** черновые поручения.
- `409 RECORDING_PROCESSING` — идёт обработка; `409 MEETING_CONFIRMED` — протокол подтверждён.
- `413 FILE_TOO_LARGE`, `422 AUDIO_INVALID`, `422 AUDIO_TOO_LONG` — проверяются синхронно до постановки в обработку (ffprobe).

### `GET /meetings/{id}/recordings/{recording_id}`

Редактор. `200` RecordingStatus. Для polling.

### `POST /meetings/{id}/recordings/{recording_id}/retry`

Редактор, встреча `draft`. Тело не требуется. `202` RecordingStatus с увеличенным `generation`.

- `processing_status = "error"` → полный повтор pipeline.
- `processing_status = "done"` и `extraction.status = "error"` → повтор только извлечения поручений и саммари по сохранённому транскрипту.
- Иначе → `409 RETRY_NOT_ALLOWED`.
- Результаты предыдущей попытки заменяются атомарно; поздние результаты устаревшей попытки отбрасываются по `generation`.

### `GET /meetings/{id}/recordings/{recording_id}/audio`

Редактор (или участник после подтверждения). Нормализованный WAV 16 kHz mono, `audio/wav`, поддерживается `Range`. Для прослушивания evidence используйте `<audio src="/api/...audio">` и `currentTime = utterance.start`.

---

## 6. Проверка протокола (редактор, только `draft`)

Каждое успешное изменение увеличивает `draft_revision`. После подтверждения — `409 MEETING_CONFIRMED`.

### `PATCH /meetings/{id}/speakers`

```json
{"SPEAKER_00": "5c1e0c0a-...", "SPEAKER_02": null}
```

- Значение — UUID участника встречи или `null` (явно «Неизвестный»). Одного сотрудника можно назначить нескольким меткам.
- Неизвестная метка → `422 VALIDATION_ERROR`; сотрудник не участник → `422 VALIDATION_ERROR`. Нет активной обработанной записи → `409 RECORDING_NOT_READY`.

`200`:

```json
{"speakers": [/* Speaker */], "draft_revision": 5}
```

### `PATCH /meetings/{id}/summary`

```json
{"summary": "Обсудили бюджет..."}
```

`200` `{"summary": "...", "summary_edited": true, "draft_revision": 6}`.

### `POST /meetings/{id}/tasks`

Ручное добавление поручения:

```json
{"task": "Согласовать смету", "to": "5c1e...", "from": "7b2d...", "deadline": "2026-09-30", "deadline_source": null, "evidence": null, "source_utterance_ids": []}
```

Обязательно только `task`. `from` — UUID участника, `SPEAKER_XX` активной записи или `null`. `201` Task (`origin: "manual"`, `confidence: null`).

### `PATCH /tasks/{id}`

Два непересекающихся режима:

1. **Редактор**, встреча `draft`: любые из `{"task", "to", "from", "deadline", "deadline_source", "evidence", "source_utterance_ids", "needs_review"}`. `to` — участник или `null`. `200` Task.
2. **Исполнитель** (пользователь, чей `employee_id` = `to`), задача `confirmed`: только `{"execution_status": "completed" | "in_progress"}`. `200` Task. Для черновой задачи → `409 TASK_NOT_CONFIRMED`. Редактор/admin тоже может менять `execution_status` подтверждённой задачи.

Смешение режимов или поля вне режима → `403 FORBIDDEN`. Чужая задача, видимая только по ID → `404`.

### `DELETE /tasks/{id}`

Редактор, встреча `draft`. `204`.

### `POST /meetings/{id}/confirm`

```json
{"draft_revision": 6, "acknowledge_incomplete": false}
```

Атомарно (одна транзакция с блокировкой встречи): все поручения → `confirmed`, `approval_status = "confirmed"`, `protocol_version = 1`, привязки спикеров фиксируются, создаются уведомления `task_assigned` исполнителям с аккаунтами. Повторный вызов не создаёт дублей.

Ошибки:

- `409 ALREADY_CONFIRMED`;
- `409 DRAFT_REVISION_MISMATCH` — `details.current_draft_revision`;
- `409 RECORDING_NOT_READY` — нет записи в статусе `done`;
- `409 CONFIRMATION_REQUIRES_ACKNOWLEDGEMENT` — при `acknowledge_incomplete = false` есть поручения без исполнителя/срока, неразрешённые спикеры или ошибка извлечения:

```json
{"error": {"code": "CONFIRMATION_REQUIRES_ACKNOWLEDGEMENT", "message": "Есть незаполненные поля", "details": {"tasks_without_assignee": ["e5b0..."], "tasks_without_deadline": [], "unresolved_speakers": ["SPEAKER_02"], "extraction_error": false}}}
```

`200` MeetingDetail.

---

## 7. Поручения

### `GET /tasks`

| Параметр | Значение |
| --- | --- |
| `assignee` | `me` (по умолчанию) или UUID сотрудника (только admin) |
| `meeting_id` | фильтр по встрече |
| `execution_status` | `in_progress` \| `completed` |
| `overdue` | `true` \| `false` |
| `limit`, `offset` | пагинация |

Возвращает только **подтверждённые** поручения. Черновые поручения доступны только редакторам через `GET /meetings/{id}`. Сортировка: по `deadline` (null в конце), затем `created_at`. `200` страница Task.

---

## 8. Уведомления

Notification:

```json
{
  "id": "1a2b...",
  "event_type": "task_assigned",
  "title": "Вам назначено поручение",
  "message": "Подготовить отчёт по бюджету — срок 2026-09-25",
  "meeting_id": "c9e1...",
  "task_id": "e5b0...",
  "created_at": "2026-09-23T10:00:00Z",
  "read_at": null
}
```

`event_type` и получатели:

| event_type | Получатель | Когда |
| --- | --- | --- |
| `meeting_invitation` | Участники с аккаунтами | Создание встречи / добавление участника |
| `protocol_ready` | Организатор и секретарь | Обработка завершена |
| `processing_failed` | Организатор и секретарь | Обработка или извлечение завершились ошибкой |
| `task_assigned` | Исполнитель | Подтверждение протокола |
| `deadline_soon` | Исполнитель и организатор | За `REMINDER_LEAD_HOURS` (24) до `deadline_at`, если не выполнено |
| `task_overdue` | Исполнитель и организатор | После `deadline_at`, если не выполнено |

Черновые поручения уведомлений не создают. Каждое событие уникально по `dedup_key` (получатель + тип + объект + версия срока/generation); повтор confirm, тика планировщика или рестарт не создают дублей.

| Метод и путь | Тело/параметры | Ответ |
| --- | --- | --- |
| `GET /notifications` | `unread_only` (bool), `limit`, `offset` | `200` `{"items": [...], "total": 3, "limit": 50, "offset": 0, "unread_count": 2}`; только свои |
| `PATCH /notifications/{id}` | `{"read": true}` или `{"read": false}` | `200` Notification; чужое → `404` |
| `POST /notifications/read-all` | — | `200` `{"updated": 2}` |

---

## 9. Экспорт

### `GET /meetings/{id}/export?fmt=docx|pdf&include_transcript=false`

- Подтверждённый протокол — участникам и редакторам. Черновик — только редакторам; документ явно помечен «ЧЕРНОВИК», имя файла с суффиксом `_draft`.
- Содержит: название, дату/время в часовом поясе встречи, повестку, участников, саммари, поручения (автор, исполнитель, срок, исходная формулировка срока, evidence), при `include_transcript=true` — транскрипт с таймкодами. Используются сохранённые правки, LLM не вызывается.
- `200` файл: `application/vnd.openxmlformats-officedocument.wordprocessingml.document` или `application/pdf`, `Content-Disposition: attachment; filename*=UTF-8''...`.
- `503 EXPORT_UNAVAILABLE` — нет LibreOffice (PDF).

---

## 10. Система

| Метод и путь | Доступ | Ответ |
| --- | --- | --- |
| `GET /health` | без авторизации | `200` `{"status": "ok", "database": "ok"}`; `503` при недоступной БД |
| `GET /system/status` | admin | Состояние моделей и зависимостей, см. ниже |

```json
{
  "models": {
    "asr": {"status": "ready", "model_id": "faster-whisper-large-v3", "error": null},
    "diarization": {"status": "error", "model_id": "pyannote/speaker-diarization-3.1", "error": "config.yaml not found in /models/pyannote/speaker-diarization-3.1"},
    "embedding": {"status": "not_loaded", "model_id": "pyannote/wespeaker-voxceleb-resnet34-LM", "error": null}
  },
  "llm": {"model": "llama3.1:8b", "endpoint_allowed": true, "reachable": true, "error": null},
  "tools": {"ffmpeg": true, "ffprobe": true, "soffice": true},
  "voice": {"model_id": "pyannote/wespeaker-voxceleb-resnet34-LM", "revision": "local", "match_threshold": 0.55, "match_margin": 0.08, "min_segment_seconds": 1.5},
  "jobs": {"max_concurrent": 1, "running": 0, "queued": 0}
}
```

`models.*.status`: `not_loaded` | `loading` | `ready` | `error`.

---

## Журнал изменений

| Версия | Дата | Изменение | Совместимость |
| --- | --- | --- | --- |
| 1.0 | 2026-09-23 | Первая фиксация | — |
| 1.3.0 | 2026-09-23 | Live-запись из браузера и `meeting_url` (`PATCH /meetings/{id}/link`) — см. `docs/LIVE_CONTRACT.md` | Добавочно: новое необязательное поле `meeting_url` во встрече |
| 1.2.0 | 2026-09-23 | `DELETE /employees/{id}` (архивирование), `can_delete` в Employee, коды `SELF_DELETE_FORBIDDEN`, `LAST_ADMIN`, `EMPLOYEE_ARCHIVED` | Добавочно |
| 1.1.0 | 2026-09-23 | Учётные записи: `must_change_password`, `POST /auth/change-password`, `POST /users/{id}/reset-password`, временный пароль генерирует backend (`password` убран из `POST/PATCH /users`), `403 PASSWORD_CHANGE_REQUIRED`, `GET /employees/me`, `me` в `/employees/{id}/voice`, `Cache-Control: no-store` | `/users` frontend не использует; новые поля пользователя — добавочные; frontend должен обработать `must_change_password` и `PASSWORD_CHANGE_REQUIRED` |
| 1.0.1 | 2026-09-23 | Добавлены значения `Speaker.review_reasons: split_cluster_suspected` и `Task.review_reasons: deadline_before_meeting` (их выдаёт AI-пайплайн, см. `docs/AI_CONTRACT.md`) | Обратно совместимо: поля — массивы строк, форма ответов не изменилась |
