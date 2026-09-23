# DARAI — контракт live-записи (backend ↔ frontend)

Версия **1.1**, 2026-09-23 (1.1: `received_audio_seconds`, `lag_seconds`, preview по времени, публикация после каждого окна). Дополняет `docs/API_CONTRACT.md` (формат ошибок, cookie-сессия, `X-CSRF-Token`, UUID, `/api`-префикс только на стороне frontend). Все операции — только **редактор встречи** (admin, организатор, секретарь); чужая/недоступная встреча → `404`, участник без прав редактора → `403 FORBIDDEN`.

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
  "received_audio_seconds": 14.1,
  "lag_seconds": 1.8,
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
- `processed_until_seconds` — до какого момента аудио реально распознано preview (не «принято»).
- `received_audio_seconds` — **измеренная** длительность декодируемого аудио в последнем снимке, который видел AI (не оценка по байтам). `null`, пока снимок ещё не декодировался.
- `lag_seconds = max(0, received_audio_seconds − processed_until_seconds)`; `null`, пока `received_audio_seconds` неизвестна. `received_audio_seconds` обновляется при каждой попытке preview и может отставать от `received_bytes`.

### Как обновляется preview (backend)

Три разных счётчика: `received_bytes` (сохранено на диск), `received_audio_seconds` (декодировано из снимка), `processed_until_seconds` (распознано). Один вызов AI не означает, что весь снимок распознан.

- На сессию — один фоновый поток preview (не задача на каждый чанк); повторные чанки только будят его. Не больше одной обработки на сессию; на процесс — одна обработка preview одновременно, тяжёлые модели под `ml.compute_lock`; во время полной обработки записи preview ждёт.
- Новые данные проверяются не чаще раза в `LIVE_PREVIEW_CHECK_INTERVAL_MS` (2000). Порога в байтах по сути нет (`LIVE_PREVIEW_MIN_NEW_BYTES`, по умолчанию 1): достаточно ли речи для ASR, решает AI (`LIVE_PREVIEW_MIN_NEW_SECONDS`).
- После **каждого** окна ASR результат сохраняется сразу (`revision` +1), не дожидаясь конца вызова.
- AI сообщил `has_pending_audio` (есть необработанный backlog в том же снимке) и продвинулся → следующий вызов сразу, с тем же снимком, без нового чанка.
- AI вернул `waiting` без backlog (мало речи) → поток ждёт новый чанк; повтор на тех же байтах не выполняется.
- AI вернул `retry_after_ms` (модель занята) → один отложенный повтор через указанное время (ограничено `LIVE_PREVIEW_RETRY_MAX_MS`), без busy-loop и без новых фоновых задач.
- Ошибка preview (`unavailable`) → следующая попытка не раньше `LIVE_PREVIEW_ERROR_BACKOFF_MS` и только на новых байтах.
- `cancel`/`finish`/смена состояния/рестарт останавливают поток; результаты, пришедшие после этого, не публикуются (проверка состояния под блокировкой строки).

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

`LIVE_MAX_CHUNK_BYTES` (5 MiB), `MAX_UPLOAD_MB` на сессию, `MAX_AUDIO_SECONDS` проверяется при `finish`, `LIVE_IDLE_TIMEOUT_SECONDS` (120), `LIVE_PREVIEW_CHECK_INTERVAL_MS` (2000), `LIVE_PREVIEW_MIN_NEW_BYTES` (1), `LIVE_PREVIEW_RETRY_MAX_MS` (30000), `LIVE_PREVIEW_ERROR_BACKOFF_MS` (10000), `LIVE_PREVIEW_MIN_NEW_SECONDS` (AI) — объединение обновлений preview; одна активная сессия на встречу; preview выполняется не чаще одного задания на сессию и пропускается, пока идёт полная обработка.
