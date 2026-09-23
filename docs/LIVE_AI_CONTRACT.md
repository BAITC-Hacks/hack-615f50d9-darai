# DARAI — Python-интерфейс live preview (backend ↔ AI)

Статус: **1.0, согласован AI-агентом 2026-09-23** (исходный черновик 0.1 — backend-агент). Источник истины по полям — `backend/app/live_ai_types.py`. Все правила `docs/AI_CONTRACT.md` действуют: нет FastAPI, SQLAlchemy и БД; сеть только локальная; содержимое не логируется.

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
))
```

Функция синхронная и блокирующая, вызывается в фоновом потоке. Не больше одного вызова на сессию одновременно.

### Снимок файла (граница фиксации)

Backend не передаёт растущий файл. Перед вызовом он копирует первые `stable_bytes` байт накопленного контейнера (все чанки `0..k`, записанные с `fsync`) в отдельный файл и передаёт его путь. После вызова снимок можно удалить. Годится и готовый WAV/PCM-файл (FFmpeg определит формат). AI читает только этот снимок.

Снимок — префикс одного контейнера MediaRecorder. Обрезанный последний кластер — норма: декодируется всё, что декодируется. Префикс из одного заголовка даёт `preview_status="waiting"` без ошибки.

### Состояние

AI-слой не хранит состояние ни в памяти процесса, ни в БД. Всё нужное лежит в `LivePreviewResult.state`. Backend хранит последний результат (в памяти или как JSON через `result.to_dict()` / `LivePreviewResult.from_dict(d)`) и передаёт его в `previous`. Потеря `previous` не ломает запись: preview начнётся заново с нуля.

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
    asr_seconds: float,               # время ASR в этом вызове
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
| Нет новых ≥ `LIVE_PREVIEW_MIN_NEW_SECONDS` декодируемых секунд | `waiting`, прежний snapshot |
| Идёт финальная обработка (занят `ml.compute_lock`) | `waiting` сразу, без ожидания |
| Нет весов ASR, сбой FFmpeg или ASR | `unavailable` + `preview_error` (`MODEL_UNAVAILABLE`, `FFMPEG_FAILED`, `ASR_FAILED`, `AUDIO_INVALID`), прежние реплики сохранены, **исключения нет** |
| `is_cancelled()` → True | `AIError("CANCELLED")` — единственное исключение |

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
