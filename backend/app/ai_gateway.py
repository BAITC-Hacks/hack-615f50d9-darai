"""Backend-side access point to the AI pipeline (docs/AI_CONTRACT.md).

Contains no AI logic: it only locates ``app.ai_pipeline`` (owned by the AI
agent) and builds its request objects from ``app.ai_types``. If the pipeline
cannot be imported, callers get AIUnavailable and store a diagnosable
MODEL_UNAVAILABLE error — there is no fallback result at runtime.

Tests may install an explicitly named test adapter with ``set_test_adapter``.
"""

from __future__ import annotations

import importlib
import threading
from types import SimpleNamespace
from typing import Any

from .config import get_settings


class AIUnavailable(Exception):
    def __init__(self, message: str):
        super().__init__("MODEL_UNAVAILABLE")
        self.code = "MODEL_UNAVAILABLE"
        self.message = message


_lock = threading.Lock()
_test_adapter: Any | None = None
_import_error: str | None = None


def set_test_adapter(adapter: Any | None) -> None:
    """Tests only: route calls to a fake adapter with the same interface."""
    global _test_adapter
    _test_adapter = adapter


def _load() -> tuple[Any, Any]:
    """Return (pipeline, types_module)."""
    global _import_error
    if _test_adapter is not None:
        return _test_adapter, _test_adapter.types
    with _lock:
        try:
            mod = importlib.import_module("app.ai_pipeline")
            types = importlib.import_module("app.ai_types")
        except Exception as exc:  # ImportError or a broken module
            _import_error = f"AI-пайплайн недоступен: {type(exc).__name__}: {str(exc)[:200]}"
            raise AIUnavailable(_import_error)
        _import_error = None
    pipeline = mod.get_pipeline() if hasattr(mod, "get_pipeline") else mod
    return pipeline, types


def types() -> Any:
    return _load()[1]


def pipeline() -> Any:
    return _load()[0]


def voice_model_info() -> SimpleNamespace:
    """Model identity used for profile compatibility. Falls back to config when
    the pipeline is not installed (no weights are loaded either way)."""
    try:
        info = pipeline().voice_model_info()
        return SimpleNamespace(model_id=info.model_id, revision=info.revision, dimension=int(info.dimension))
    except AIUnavailable:
        s = get_settings()
        return SimpleNamespace(model_id=s.voice_model_id, revision=s.voice_model_revision,
                               dimension=s.voice_embedding_dim)


def models_status() -> dict:
    try:
        return pipeline().models_status()
    except AIUnavailable as exc:
        return {k: {"status": "error", "model_id": None, "error": exc.message}
                for k in ("asr", "diarization", "embedding")}


def preload() -> None:
    try:
        pipeline().preload()
    except AIUnavailable:
        pass
