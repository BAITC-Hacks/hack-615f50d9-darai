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

`app.ml.compute_lock` — один процессный lock на тяжёлые CPU-задачи. `process_recording` ждёт его. Preview берёт его без ожидания, а если lock занят, возвращает `waiting` с `retry_after_ms`. Lock держится весь вызов, включая `on_update`. Регистрация голоса (лёгкая, только WeSpeaker) его не берёт. Backend дополнительно не запускает preview во время полной обработки. Двойная защита безопасна.

## Финализация

После `finish` backend вызывает `ai_pipeline.process_recording(ProcessRequest(source_path=<полный контейнер>, ...))`, как для загруженного файла. Строгость — у существующей нормализации: ffprobe/ffmpeg с проверкой кода возврата и длительности. Preview в финализацию не передаётся. Пока нет gated-весов диаризации, финализация завершается `AIError("MODEL_UNAVAILABLE", stage="diarizing", model="diarization")`. Проверено `scripts/ai/live_simulate.py --final`: фиктивной диаризации нет.

## Алгоритм (для справки)

1. FFmpeg декодирует снимок (первые `stable_bytes`) в 16 kHz mono (`-err_detect ignore_err`). Декодируется весь префикс, но распознаётся только новое аудио.
2. Модель: слот `live_asr` (`LIVE_ASR_MODEL_PATH`, например large-v3-turbo), если он задан. Иначе явно используется слот `asr` (large-v3 финальной обработки). Заданная, но отсутствующая live-модель даёт `MODEL_UNAVAILABLE` (`model=live_asr`) без подмены. Модель загружается один раз на процесс.
3. Окно `[q, q + LIVE_PREVIEW_WINDOW_SECONDS]`, где `q` — самая тихая 20-мс точка около `committed_until − LIVE_PREVIEW_CONTEXT_SECONDS`, но не позже `committed_until`. Хвост короче `LIVE_PREVIEW_MIN_NEW_SECONDS` присоединяется к последнему окну. До `LIVE_PREVIEW_MAX_WINDOWS_PER_CALL` окон за вызов, после **каждого** — `on_update`.
4. Язык. Язык встречи `ru`/`kk` передаётся как есть и никогда не переопределяется. `auto`: окно кодируется энкодером один раз, язык выбирается среди `ASR_ALLOWED_LANGUAGES` по этому выходу, декодер получает тот же выход. Штатный auto-режим faster-whisper кодирует окно дважды. Перевода нет: `task=transcribe`.
5. Окно без речи (Silero VAD, тот же, что внутри transcribe) пропускается без вызова ASR.
6. «Дыры»: если на озвученном (по энергии) участке ≥ `LIVE_PREVIEW_HOLE_MIN_SECONDS` модель не вернула слов, участок распознаётся отдельно со своим языком (в `auto`), не больше `LIVE_PREVIEW_MAX_HOLES_PER_WINDOW` на окно. Так не теряется казахская фраза внутри «русского» окна.
7. Слово новое, если его конец позже `committed_until`. Первое слово, пересекающее границу и совпадающее с одним из трёх последних зафиксированных, — повтор из перекрытия, отбрасывается. То же слово, сказанное позже, сохраняется.
8. Фиксируются слова, кончающиеся до `край − LIVE_PREVIEW_GUARD_SECONDS` (у живого края) или до `конец окна − LIVE_PREVIEW_OVERLAP_SECONDS` (backlog). Через паузу позиция переходит только по тишине энергетического VAD: речь без слов остаётся следующему окну.
9. Сегменты с `no_speech_prob > LIVE_PREVIEW_NO_SPEECH_PROB` отбрасываются. **Чёрного списка фраз нет**: настоящие «Спасибо за внимание», «Продолжение следует» не удаляются.
10. Реплика закрывается по паузе > `LIVE_PREVIEW_UTTERANCE_GAP_SECONDS` или по концу предложения.

LLM в live-пути не вызывается.

## Env (переносит backend в `.env.example`)

Все читаются через `os.environ` в `app/live_ai.py` / `app/ml.py`, `config.py` не меняется. Пустое или нечисловое значение означает дефолт.

| Переменная | Дефолт в коде | Рекомендуется (M4 Pro, измерено) | Смысл |
| --- | --- | --- | --- |
| `LIVE_ASR_MODEL_PATH` | пусто = слот `asr` (large-v3) | `faster-whisper-large-v3-turbo` | Live-модель; абсолютный путь или относительно `MODELS_DIR`. Готовится `prepare_models.py --only live_asr` |
| `LIVE_ASR_MODEL_ID` | имя каталога | — | Для `models_status()` |
| `LIVE_ASR_COMPUTE_TYPE` | = `ASR_COMPUTE_TYPE` | `int8` | |
| `LIVE_ASR_DEVICE` | = `ASR_DEVICE` | `cpu` | |
| `LIVE_ASR_CPU_THREADS` | = `ASR_CPU_THREADS` | `8` | 8 потоков: окно turbo 5.2 → 3.2 с; 12 не быстрее. В Docker нужно ≥ 8 CPU |
| `LIVE_PREVIEW_WINDOW_SECONDS` | `12` | `12` | Максимум окна. Стоимость окна почти постоянна (энкодер 30 с), окно ограничивает только backlog; 8 с не быстрее |
| `LIVE_PREVIEW_OVERLAP_SECONDS` | `1.5` | | Не фиксируемый хвост backlog-окна |
| `LIVE_PREVIEW_CONTEXT_SECONDS` | `0.5` | | Аудио перед зафиксированной точкой в начале окна |
| `LIVE_PREVIEW_GUARD_SECONDS` | `1.5` | | Хвост у живого края остаётся черновым |
| `LIVE_PREVIEW_MIN_NEW_SECONDS` | `3` | | Меньше нового аудио — `waiting`; порог `has_pending_audio` |
| `LIVE_PREVIEW_MAX_WINDOWS_PER_CALL` | `4` | | Окон за вызов (каждое публикуется) |
| `LIVE_PREVIEW_BUSY_RETRY_MS` | `1000` | | `retry_after_ms` при занятом `compute_lock` |
| `LIVE_PREVIEW_RESTRICT_LANGUAGES` | `true` | | `auto`: язык окна только из `ASR_ALLOWED_LANGUAGES`, один проход энкодера |
| `LIVE_PREVIEW_HOLE_MIN_SECONDS` | `0.8` | | Минимальная озвученная «дыра» для повторного распознавания |
| `LIVE_PREVIEW_MAX_HOLES_PER_WINDOW` | `2` | | `0` — выключить |
| `LIVE_PREVIEW_UTTERANCE_GAP_SECONDS` | `1.0` | | Пауза, закрывающая реплику |
| `LIVE_PREVIEW_MAX_UTTERANCE_SECONDS` | `20` | | Длинная реплика режется на конце предложения |
| `LIVE_PREVIEW_NO_SPEECH_PROB` | `0.6` | | Порог отбрасывания сегмента |
| `LIVE_PREVIEW_HALLUCINATION_SILENCE_SECONDS` | `1.0` | | `hallucination_silence_threshold`; `0` — выключить |
| `LIVE_PREVIEW_BEAM_SIZE` | пусто = `ASR_BEAM_SIZE` | пусто (5) | beam 1 на turbo не дал ускорения (3.3 vs 3.2 с/окно, энкодер доминирует) |
| `LIVE_PREVIEW_REFINE_LANGUAGES` | `false` | `false` | Переопределение языка по сегментам (дорого, отключает п.4) |

Финальная обработка этих переменных не читает: `ASR_*` и слот `asr` (large-v3) не меняются.

## Что нужно от backend (1.1)

1. Передавать `on_update`: сохранять `result.to_dict()` в `preview_state`, реплики, `processed_until_seconds`, увеличивать `revision`. Ошибки сохранения поднимать как есть: AI их не перехватывает.
2. После вызова: `has_pending_audio` → вызвать снова сразу, даже без новых байт. `retry_after_ms` → повтор через это время (≤ `LIVE_PREVIEW_RETRY_MAX_MS`). Иначе ждать новые байты. Сейчас `_preview_loop` выходит при `stable − preview_bytes < min_new_bytes`, и backlog без новых чанков не дорабатывается.
3. Передавать снимок-префикс контейнера, никогда отдельный чанк.
4. Прогревать `live_asr` на старте, если `LIVE_ASR_MODEL_PATH` задан (`get_registry().get("live_asr")`), и показывать `models_status()["live_asr"]` (`not_configured` | `ready` | `error`).
5. Перенести env из таблицы в `.env.example`; в Docker выделить ≥ 8 CPU.
