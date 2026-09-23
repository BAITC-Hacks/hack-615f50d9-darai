# DARAI — Python-интерфейс live preview (backend ↔ AI)

Статус: **черновик 0.1 backend-агента** (2026-09-23), ждёт подтверждения AI-агента. AI-агент реализует `backend/app/live_ai_types.py` и `backend/app/live_ai.py`. Backend вызывает только `app.live_ai.transcribe_preview`. Правила `docs/AI_CONTRACT.md` действуют (без FastAPI/SQLAlchemy/БД, без сети кроме локальной, без логов содержимого, ошибки — `AIError` из `app.ai_types`).

Финальная обработка **не** меняется: после `finish` backend вызывает существующий `ai_pipeline.process_recording` на полном контейнере.

## Типы (`app/live_ai_types.py`)

```python
@dataclass(frozen=True)
class LivePreviewUtterance:
    id: str                 # стабильный в пределах сессии, напр. "preview-0"
    start: float            # секунды от начала записи
    end: float
    speaker_label: str | None   # может быть None
    text: str
    is_final: bool          # True: сегмент больше не изменится


@dataclass(frozen=True)
class LivePreviewResult:
    processed_until_seconds: float        # до какого момента аудио обработано
    utterances: list[LivePreviewUtterance]  # ПОЛНЫЙ список (не дельта)


@dataclass(frozen=True)
class LivePreviewRequest:
    container_path: Path     # копия первых stable_bytes байт контейнера (только чтение)
    mime_type: str           # "audio/webm;codecs=opus" и т.п.
    stable_bytes: int        # размер снимка
    work_dir: Path           # каталог для временных файлов AI (backend удаляет при отмене)
    previous: LivePreviewResult | None   # прошлый результат этой сессии
    is_cancelled: Callable[[], bool]     # True -> прекратить (AIError("CANCELLED"))
```

## Функция (`app/live_ai.py`)

```python
def transcribe_preview(req: LivePreviewRequest) -> LivePreviewResult: ...
```

- Синхронная, блокирующая; backend вызывает в фоновом потоке, не чаще одного вызова на сессию, пропускает вызовы во время полной обработки.
- Снимок — префикс одного контейнера MediaRecorder: хвост может быть обрезан. Декодировать то, что декодируется (FFmpeg обычно читает обрезанный WebM/Ogg до последнего целого кластера); неполный хвост — **не ошибка**.
- Может переиспользовать `previous` (не перераспознавать то, что уже `is_final`).
- Ошибки: `AIError("MODEL_UNAVAILABLE"| "ASR_FAILED" | "AUDIO_INVALID" | "CANCELLED" | …)`. Backend показывает их как `preview_status="unavailable"` + `preview_error`; сессия записи продолжается.
- Без диаризации и LLM допустимо (`speaker_label=None`).
