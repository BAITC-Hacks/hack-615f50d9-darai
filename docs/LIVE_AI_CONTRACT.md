# DARAI — Python-интерфейс live preview (backend ↔ AI)

Статус: **1.1 (2026-09-23): инкрементальная выдача `on_update`, `has_pending_audio`, `retry_after_ms`, отдельная live-модель.** 1.0 согласован AI-агентом 2026-09-23 (исходный черновик 0.1 — backend-агент). Всё из 1.0 совместимо: новые поля необязательны. Источник истины по полям — `backend/app/live_ai_types.py`. Все правила `docs/AI_CONTRACT.md` действуют: нет FastAPI, SQLAlchemy и БД; сеть только локальная; содержимое не логируется.

Preview — черновой текст во время записи. Протокол строится только после `finish`: полный контейнер проходит существующий `ai_pipeline.process_recording` (строгая нормализация, ASR, диаризация, идентификация, поручения, саммари). **Финальный транскрипт заменяет preview целиком.** Id preview (`preview-N`) не связаны с id реплик протокола, переносить их нельзя.

## Вызов

```python
from app.live_ai import transcribe_preview
from app.live_ai_types import LivePreviewRequest, LivePreviewResult

result: LivePreviewResult = transcribe_preview(LivePreviewRequest(
    container_path=snapshot_path,      # ЗАФИКСИРОВАННЫЙ снимок, см. ниже
    mime_type="audio/webm;codecs=opus",
    stable_bytes=n,
    work_dir=session_dir / "preview-ai",
    previous=last_result,              # None при первом вызове
    is_cancelled=lambda: session_cancelled(),
    asr_language=meeting.asr_language, # "auto" | "ru" | "kk" | None (= ASR_LANGUAGE)
    asr_profile=meeting.asr_profile,
    on_update=publish,                 # НОВОЕ: вызывается после КАЖДОГО окна
))
```

Функция синхронная и блокирующая, вызывается в фоновом потоке. Не больше одного вызова на сессию одновременно.

### Инкрементальная выдача (1.1)

- `on_update(result)` вызывается сразу после каждого распознанного окна, в том же потоке, до распознавания следующего окна. `result` — полный snapshot (как возвращаемое значение): `utterances`, `state`, `processed_until_seconds`, `has_pending_audio`, `preview_status="ready"`. Backend сохраняет его (`to_dict()`), публикует и запоминает как `previous` следующего вызова.
- Возвращается последний результат (тот же, что получил последний `on_update`, или `waiting`/`unavailable`).
- Исключение из `on_update` (например, ошибка записи в БД) пробрасывается из `transcribe_preview` **как есть**. Оно не превращается в `ASR_FAILED`/`unavailable`. `compute_lock` освобождается.
- Если после N окон случилась ошибка ASR, возвращается `unavailable` со snapshot после окна N, а не со старым `previous`.
- `is_cancelled()` проверяется между окнами. Уже отданные через `on_update` окна остаются действительными.
- `on_update` вызывается под `ml.compute_lock`: он должен быть коротким (одна транзакция), без ожидания других тяжёлых задач.

### Когда вызывать снова

| Результат | Что делать backend |
| --- | --- |
| `has_pending_audio=True` | Снова вызвать **сразу**, можно с тем же или более новым снимком. В снимке остался backlog ≥ `LIVE_PREVIEW_MIN_NEW_SECONDS`; новые чанки ждать не нужно |
| `retry_after_ms=N` (`waiting`) | Модель занята (финальная обработка или другое preview). Повторить через N мс (backend ограничивает `LIVE_PREVIEW_RETRY_MAX_MS`) |
| `waiting`, `retry_after_ms=None`, `has_pending_audio=False` | Мало нового аудио. Ждать новый чанк. Это не «модель занята» |
| `unavailable` | См. `preview_error`; backoff `LIVE_PREVIEW_ERROR_BACKOFF_MS` |

`has_pending_audio` считается только по этому снимку: `decoded_seconds − processed_until_seconds ≥ LIVE_PREVIEW_MIN_NEW_SECONDS`. Необработанная речь не считается завершённой: пока она не распознана, `processed_until_seconds` меньше `decoded_seconds`.

### Снимок файла (граница фиксации)

Backend не передаёт растущий файл. Перед вызовом он копирует первые `stable_bytes` байт накопленного контейнера (все чанки `0..k`, записанные с `fsync`) в отдельный файл и передаёт его путь. После вызова снимок можно удалить. Годится и готовый WAV/PCM-файл (FFmpeg определит формат). AI читает только этот снимок и не дальше `stable_bytes` (если файл длиннее, берётся префикс).

**Отдельный чанк MediaRecorder — не файл:** без заголовка первого чанка он не декодируется. Если снимок декодируется короче, чем уже было декодировано (`decoded_seconds` из `previous`) больше чем на 1 с, результат — `unavailable` + `AUDIO_INVALID` («нужен префикс того же контейнера»), прежние реплики сохранены.

Снимок — префикс одного контейнера MediaRecorder. Обрезанный последний кластер — норма: декодируется всё, что декодируется. Префикс из одного заголовка даёт `preview_status="waiting"` без ошибки.

### Состояние

AI-слой не хранит состояние ни в памяти процесса, ни в БД. Всё нужное лежит в `LivePreviewResult.state`. Backend хранит последний результат (в памяти или как JSON через `result.to_dict()` / `LivePreviewResult.from_dict(d)`) и передаёт его в `previous`. `from_dict` принимает JSON версии 1.0 (без новых полей и даже без `state`). Если `state` пуст, а в `previous.utterances` есть финальные реплики, распознавание продолжается после конца последней финальной реплики, а не с нуля; нефинальный хвост распознаётся заново. Потеря `previous` целиком: preview начнётся с нуля.

## Результат

```python
LivePreviewResult(
    processed_until_seconds: float,   # до какого момента аудио реально распознано (не «принято»)
    utterances: list[LivePreviewUtterance],  # ПОЛНЫЙ snapshot, не дельта
    preview_status: "waiting" | "ready" | "unavailable",
    preview_error: {"code", "message"} | None,
    draft_tasks: list,                # всегда [] (P1 не реализован)
    state: LivePreviewState,          # вернуть как previous
    decoded_seconds: float,           # сколько аудио декодируется из снимка
    asr_seconds: float,               # время ASR в этом вызове (в on_update — накопленное до этого окна)
    has_pending_audio: bool = False,  # 1.1: в ЭТОМ снимке остался backlog, вызвать снова сразу
    retry_after_ms: int | None = None,# 1.1: только при занятой модели/lock
    asr_model: str | None = None,     # 1.1: "live_asr" | "asr" — какой слот распознавал (диагностика)
)
LivePreviewUtterance(id="preview-N", start, end, speaker_label=None, text, is_final)
```

Соответствие HTTP (`docs/LIVE_CONTRACT.md`): `processed_until_seconds`, `preview_status`, `preview_error`, `utterances`, `draft_tasks` отдаются как есть. Статус `processing` backend ставит сам на время вызова.

- `is_final=true`: реплика больше не изменится, её `id` постоянен.
- `is_final=false`: последняя открытая реплика и хвост у живого края. Текст может уточниться при следующем вызове, `id` по возможности сохраняется.
- Таймкоды — секунды от начала записи (от начала контейнера), общие для всех вызовов, реплики упорядочены и не пересекаются.
- `speaker_label` всегда `null`: live-диаризации нет, спикеры появятся после финальной обработки.

## Ошибки

| Ситуация | Поведение |
| --- | --- |
| Нет новых ≥ `LIVE_PREVIEW_MIN_NEW_SECONDS` декодируемых секунд | `waiting`, прежний snapshot, `retry_after_ms=None`, `has_pending_audio=False` |
| Идёт финальная обработка (занят `ml.compute_lock`) | `waiting` сразу, без ожидания, `retry_after_ms=LIVE_PREVIEW_BUSY_RETRY_MS` |
| Нет весов ASR (или live-модели), сбой FFmpeg или ASR | `unavailable` + `preview_error` (`MODEL_UNAVAILABLE`, `FFMPEG_FAILED`, `ASR_FAILED`, `AUDIO_INVALID`), последние реплики сохранены, **исключения нет** |
| Снимок короче уже декодированного | `unavailable` + `AUDIO_INVALID` |
| `is_cancelled()` → True | `AIError("CANCELLED")` |
| Исключение в `on_update` | пробрасывается как есть |

Ошибка preview не влияет на запись и финализацию.

## Конкуренция

`app.ml.compute_lock` — один процессный lock на тяжёлые CPU-задачи. `process_recording` ждёт его. Preview берёт его без ожидания, а если lock занят, возвращает `waiting`. Регистрация голоса (лёгкая, только WeSpeaker) его не берёт. Backend дополнительно не запускает preview во время полной обработки. Двойная защита безопасна.

## Финализация

После `finish` backend вызывает `ai_pipeline.process_recording(ProcessRequest(source_path=<полный контейнер>, ...))`, как для загруженного файла. Строгость — у существующей нормализации: ffprobe/ffmpeg с проверкой кода возврата и длительности. Preview в финализацию не передаётся. Пока нет gated-весов диаризации, финализация завершается `AIError("MODEL_UNAVAILABLE", stage="diarizing", model="diarization")`. Проверено `scripts/ai/live_simulate.py --final`: фиктивной диаризации нет.

## Алгоритм (для справки)

1. FFmpeg декодирует снимок в 16 kHz mono (`-err_detect ignore_err`).
2. Окно `[q, q + LIVE_PREVIEW_WINDOW_SECONDS]`, где `q` — самая тихая 20-мс точка около `committed_until − LIVE_PREVIEW_OVERLAP_SECONDS` (±0.75 с). Окно, начатое внутри слова, вызывало у Whisper галлюцинацию «Продолжение следует...» на всё окно. До `LIVE_PREVIEW_MAX_WINDOWS_PER_CALL` окон подряд, пока не дойдём до живого края. Используется уже загруженная модель faster-whisper large-v3, та же стратегия языков, `vad_filter`, `hallucination_silence_threshold`.
3. Слово новое, если его конец позже `committed_until`. Первое слово, пересекающее границу и совпадающее с одним из трёх последних зафиксированных, — это повтор из overlap, он отбрасывается. То же слово, сказанное позже, сохраняется.
4. Фиксируются слова, кончающиеся до `край − LIVE_PREVIEW_GUARD_SECONDS` (у живого края) или до `конец окна − overlap` (при отставании). Через паузу позиция переходит только по участку, который энергетический VAD считает тишиной: речь, для которой модель не выдала слов, остаётся следующему окну.
5. Сегменты с `no_speech_prob > LIVE_PREVIEW_NO_SPEECH_PROB` и сегменты, целиком состоящие из известных фраз-галлюцинаций Whisper, отбрасываются.
6. Реплика закрывается по паузе > `LIVE_PREVIEW_UTTERANCE_GAP_SECONDS` или по концу предложения.

## Env (переносит backend в `.env.example`)

| Переменная | Дефолт | Смысл |
| --- | --- | --- |
| `LIVE_PREVIEW_WINDOW_SECONDS` | `24` | Окно ASR. 12 с на CPU M4 Pro не успевает (лаг растёт до ~48 с), см. `docs/AI_SETUP.md` §10 |
| `LIVE_PREVIEW_OVERLAP_SECONDS` | `1.5` | Перекрытие окон |
| `LIVE_PREVIEW_GUARD_SECONDS` | `1.5` | Хвост у живого края остаётся черновым |
| `LIVE_PREVIEW_MIN_NEW_SECONDS` | `3` | Меньше нового аудио — `waiting` |
| `LIVE_PREVIEW_MAX_WINDOWS_PER_CALL` | `3` | Ограничение длительности одного вызова |
| `LIVE_PREVIEW_UTTERANCE_GAP_SECONDS` | `1.0` | Пауза, закрывающая реплику |
| `LIVE_PREVIEW_MAX_UTTERANCE_SECONDS` | `20` | Длинная реплика режется на конце предложения |
| `LIVE_PREVIEW_NO_SPEECH_PROB` | `0.6` | Порог отбрасывания сегмента |
| `LIVE_PREVIEW_HALLUCINATION_SILENCE_SECONDS` | `1.0` | `hallucination_silence_threshold` faster-whisper; `0` — выключить |
| `LIVE_PREVIEW_BEAM_SIZE` | пусто = `ASR_BEAM_SIZE` | `1` почти не ускоряет и ухудшает текст (измерено) |
| `LIVE_PREVIEW_REFINE_LANGUAGES` | = `ASR_REFINE_LANGUAGES` | Переопределение языка по сегментам. Измерения в `docs/AI_SETUP.md` §10 |

Пустое или нечисловое значение означает дефолт. Используются также `ASR_*` (язык, VAD, разрешённые языки).
