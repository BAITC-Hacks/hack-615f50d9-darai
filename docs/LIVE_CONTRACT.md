# DARAI — контракт live-записи (backend ↔ frontend)

Версия **1.0**, 2026-09-23. Дополняет `docs/API_CONTRACT.md` (формат ошибок, cookie-сессия, `X-CSRF-Token`, UUID, `/api`-префикс только на стороне frontend). Все операции — только **редактор встречи** (admin, организатор, секретарь); чужая/недоступная встреча → `404`, участник без прав редактора → `403 FORBIDDEN`.

Транспорт: бинарные HTTP-загрузки + polling. WebSocket нет.

## Модель

`MediaRecorder.start(timeslice)` выдаёт части **одного** контейнера. Backend дописывает байты строго по `sequence` в один файл сессии и никогда не декодирует чанк отдельно. Предварительный транскрипт строится по стабильному снимку накопленного файла; незавершённый хвост контейнера не переводит сессию в `error`. После `finish` весь контейнер нормализуется FFmpeg и проходит **существующий полный pipeline**, результат — обычный черновик протокола (`GET /meetings/{id}`).

## 1. `POST /meetings/{id}/live`

```json
{"source": "microphone", "mime_type": "audio/webm;codecs=opus"}
```

`source`: `microphone` | `display`. `mime_type`: `audio/webm…`, `video/webm…`, `audio/ogg…`, `audio/mp4…`, `video/mp4…` (кодеки в параметрах допускаются).

`201`:

```json
{"session_id": "uuid", "state": "recording", "next_sequence": 0, "poll_after_ms": 2000, "max_chunk_bytes": 5242880}
```

Ошибки: `409 LIVE_ALREADY_ACTIVE` (у встречи уже есть сессия в `recording`; `details.session_id`), `409 MEETING_CONFIRMED`, `409 RECORDING_PROCESSING`, `422 VALIDATION_ERROR` (source/mime_type).
Старт не трогает существующую запись и черновик: замена — при `finish` по правилам `POST /meetings/{id}/recordings`.

## 2. `PUT /meetings/{id}/live/{session_id}/chunks/{sequence}`

Тело — бинарные байты (Blob), `Content-Type` записи. `sequence` — целое ≥ 0.

`200` — после записи на диск и `fsync`:

```json
{"accepted_sequence": 0, "next_sequence": 1}
```

| Ситуация | Ответ |
| --- | --- |
| `sequence == next_sequence` | принят, `200` |
| `sequence < next_sequence`, те же байты (sha256) | `200` без дублирования |
| `sequence < next_sequence`, другие байты | `409 CHUNK_CONFLICT` |
| `sequence > next_sequence` | `409 CHUNK_OUT_OF_ORDER`, `details.next_sequence` |
| пустое тело / больше `max_chunk_bytes` | `422 VALIDATION_ERROR` / `413 FILE_TOO_LARGE` |
| сумма больше `MAX_UPLOAD_MB` | `413 FILE_TOO_LARGE` |
| сессия не в `recording` | `409 LIVE_NOT_RECORDING`, `details.state` |

## 3. `GET /meetings/{id}/live/{session_id}`

Полный snapshot; клиент **заменяет** предыдущий.

```json
{
  "session_id": "uuid",
  "state": "recording",
  "next_sequence": 3,
  "received_bytes": 12345,
  "processed_until_seconds": 12.3,
  "revision": 2,
  "preview_status": "ready",
  "preview_error": null,
  "utterances": [
    {"id": "preview-0", "start": 0.0, "end": 4.2, "speaker_label": null, "text": "Текст", "is_final": false}
  ],
  "draft_tasks": [],
  "recording_id": null,
  "error": null
}
```

- `state`: `recording` → `finalizing` (идёт полный pipeline записи `recording_id`) → `done` | `error`; либо `cancelled`. `done`/`error` после `finalizing` вычисляются из статуса записи (`GET /meetings/{id}/recordings/{recording_id}` даёт этап и ошибку).
- `error` у `recording`-сессии: `{"code": "CLIENT_TIMEOUT"}` (нет чанков дольше `LIVE_IDLE_TIMEOUT_SECONDS`, по умолчанию 120) или `{"code": "INTERRUPTED"}` (рестарт backend). Накопленные байты сохраняются, такую сессию можно завершить `finish` (см. ниже) или отменить.
- `preview_status`: `waiting` (мало данных/ждёт очереди), `processing`, `ready`, `unavailable` (модели нет или ошибка; `preview_error = {"code","message"}`). Ошибка preview не влияет на запись и финализацию.
- `revision` растёт при каждом новом preview.
- `draft_tasks` — схема Task из API_CONTRACT; сейчас всегда `[]`. Предварительные задачи не сохраняются и не отправляются исполнителям.
- `processed_until_seconds` — до какого момента аудио покрыто preview.

## 4. `POST /meetings/{id}/live/{session_id}/finish`

```json
{"last_sequence": 2}
```

`202`: `{"session_id": "uuid", "state": "finalizing", "recording_id": "uuid"}`.

- Требует получения всех чанков `0..last_sequence`: иначе `409 CHUNKS_MISSING`, `details.next_sequence`.
- Нет данных — `422 LIVE_EMPTY`. Контейнер не распознан FFmpeg — `422 AUDIO_INVALID` (сессия остаётся, можно отменить); длительность больше лимита — `422 AUDIO_TOO_LONG`.
- Повторный `finish` не создаёт второе задание: `202` с текущим состоянием и тем же `recording_id`.
- Разрешён из `recording` и из `error` с кодом `CLIENT_TIMEOUT`/`INTERRUPTED`. Из `cancelled` → `409 LIVE_NOT_RECORDING`.
- Замена прежней записи — по правилам `POST /meetings/{id}/recordings`: `409 RECORDING_PROCESSING`, `409 MEETING_CONFIRMED`.

## 5. `POST /meetings/{id}/live/{session_id}/cancel`

Тела нет. `204`, идемпотентно. Прекращает приём и preview, удаляет временные файлы сессии. Прежний протокол/запись встречи не трогает. После `finish` отмена не останавливает полную обработку (`204`, состояние не меняется).

## Ссылка на онлайн-встречу

- `POST /meetings` (JSON и multipart) принимает необязательный `meeting_url`; MeetingListItem/MeetingDetail содержат `"meeting_url": "https://…" | null`.
- `PATCH /meetings/{id}/link` `{"meeting_url": "https://…" | null}` → `200 {"meeting_url": …}`. Только редактор; только `http`/`https`, до 2000 символов, иначе `422 VALIDATION_ERROR`. Разрешено и после подтверждения протокола. Сервер ссылку не открывает и ничего не скачивает.

## Ограничения

`LIVE_MAX_CHUNK_BYTES` (5 MiB), `MAX_UPLOAD_MB` на сессию, `MAX_AUDIO_SECONDS` проверяется при `finish`, `LIVE_IDLE_TIMEOUT_SECONDS` (120), `LIVE_PREVIEW_MIN_NEW_SECONDS`/`LIVE_PREVIEW_MIN_NEW_BYTES` — объединение обновлений preview; одна активная сессия на встречу; preview выполняется не чаще одного задания на сессию и пропускается, пока идёт полная обработка.
