"""Process-wide registry of local speech models.

Each model is loaded at most once per process (thread-safe) from a local path.
Hub downloads are disabled: a missing model is reported as MODEL_UNAVAILABLE
with a diagnosable message, never replaced by a stub.

The diarization pipeline is assembled from local checkpoints (segmentation +
the SAME WeSpeaker model object used for voice profiles), so config.yaml needs
no Hub ids and one embedding model version serves enrollment and matching.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings, get_settings

# Hard offline mode for every HF-based library; set before they are imported.
for _k, _v in {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_DATASETS_OFFLINE": "1",
    "PYANNOTE_METRICS_ENABLED": "0",
    "DO_NOT_TRACK": "1",
}.items():
    os.environ.setdefault(_k, _v)

log = logging.getLogger("darai.ml")


class ModelUnavailable(Exception):
    def __init__(self, model: str, message: str):
        super().__init__(model)
        self.model = model
        self.message = message


@dataclass
class ModelSlot:
    key: str
    model_id: str
    status: str = "not_loaded"  # not_loaded | loading | ready | error | not_configured (live_asr)
    error: str | None = None
    obj: Any = None


def live_asr_path(settings: Settings) -> Path | None:
    """Separate live-preview ASR model (e.g. large-v3-turbo), or None when not configured.

    LIVE_ASR_MODEL_PATH: absolute, or relative to MODELS_DIR. Empty = live preview uses the
    "asr" slot (the final large-v3) explicitly. A configured but missing path is an error for
    the "live_asr" slot; it is never silently replaced by another model.
    """
    raw = (os.environ.get("LIVE_ASR_MODEL_PATH") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else Path(settings.models_dir) / path


def segmentation_path(settings: Settings) -> Path:
    return settings.resolved_segmentation_path


def embedding_checkpoint(settings: Settings) -> Path:
    path = settings.resolved_embedding_path
    return path / "pytorch_model.bin" if path.suffix != ".bin" else path


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._locks = {k: threading.Lock() for k in ("asr", "live_asr", "diarization", "embedding")}
        live = live_asr_path(settings)
        self.slots = {
            "asr": ModelSlot("asr", settings.asr_model_id),
            "live_asr": ModelSlot("live_asr", os.environ.get("LIVE_ASR_MODEL_ID") or (live.name if live else ""),
                                  status="not_loaded" if live else "not_configured"),
            "diarization": ModelSlot("diarization", settings.diarization_model_id),
            "embedding": ModelSlot("embedding", settings.voice_model_id),
        }
        self._loaders: dict[str, Callable[[], Any]] = {
            "asr": self._load_asr,
            "live_asr": self._load_live_asr,
            "diarization": self._load_diarization,
            "embedding": self._load_embedding,
        }

    def get(self, key: str) -> Any:
        slot = self.slots[key]
        if slot.status == "ready":
            return slot.obj
        with self._locks[key]:
            if slot.status == "ready":
                return slot.obj
            slot.status, slot.error = "loading", None
            try:
                slot.obj = self._loaders[key]()
                slot.status = "ready"
                log.info("model loaded: %s", key)
                return slot.obj
            except ModelUnavailable as exc:
                slot.status, slot.error = "error", exc.message
                raise
            except Exception as exc:  # import errors, corrupt weights, OOM
                slot.status, slot.error = "error", f"{type(exc).__name__}: {str(exc)[:300]}"
                log.error("model load failed: %s (%s)", key, type(exc).__name__)
                raise ModelUnavailable(key, slot.error)

    def status(self) -> dict:
        return {k: {"status": s.status, "model_id": s.model_id, "error": s.error} for k, s in self.slots.items()}

    # -------------------------------------------------------------- loaders

    def torch_device(self):
        import torch

        name = self.settings.torch_device
        if name == "cuda" and not torch.cuda.is_available():
            raise ModelUnavailable("device", "TORCH_DEVICE=cuda, но CUDA недоступна")
        if name == "mps" and not torch.backends.mps.is_available():
            raise ModelUnavailable("device", "TORCH_DEVICE=mps, но MPS недоступен")
        return torch.device(name)

    def _load_asr(self):
        path = self.settings.resolved_asr_path
        if not (path / "model.bin").is_file():
            raise ModelUnavailable("asr", f"Не найдены веса faster-whisper: {path}/model.bin")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelUnavailable("asr", f"faster-whisper не установлен: {exc}")
        s = self.settings
        return WhisperModel(str(path), device=s.asr_device, compute_type=s.asr_compute_type,
                            cpu_threads=s.asr_cpu_threads, local_files_only=True)

    def _load_live_asr(self):
        path = live_asr_path(self.settings)
        if path is None:
            raise ModelUnavailable("live_asr", "LIVE_ASR_MODEL_PATH не задан")
        if not (path / "model.bin").is_file():
            raise ModelUnavailable("live_asr", f"Не найдены веса live-модели faster-whisper: {path}/model.bin")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelUnavailable("live_asr", f"faster-whisper не установлен: {exc}")
        s = self.settings
        threads = os.environ.get("LIVE_ASR_CPU_THREADS", "").strip()
        return WhisperModel(str(path), device=os.environ.get("LIVE_ASR_DEVICE") or s.asr_device,
                            compute_type=os.environ.get("LIVE_ASR_COMPUTE_TYPE") or s.asr_compute_type,
                            cpu_threads=int(threads) if threads.isdigit() else s.asr_cpu_threads,
                            local_files_only=True)

    def _embedding_model(self):
        """Raw pyannote Model for WeSpeaker; shared by diarization and voice matching."""
        ckpt = embedding_checkpoint(self.settings)
        if not ckpt.is_file():
            raise ModelUnavailable("embedding", f"Не найдены веса WeSpeaker: {ckpt}")
        try:
            from pyannote.audio import Model
        except ImportError as exc:
            raise ModelUnavailable("embedding", f"pyannote.audio не установлен: {exc}")
        model = Model.from_pretrained(str(ckpt))
        if model is None:
            raise ModelUnavailable("embedding", "pyannote не смог загрузить модель эмбеддингов")
        model.eval()
        return model

    def _load_embedding(self):
        from pyannote.audio import Inference

        return Inference(self._embedding_model(), window="whole", device=self.torch_device())

    def _load_diarization(self):
        cfg_path = self.settings.resolved_diarization_config
        seg_ckpt = segmentation_path(self.settings) / "pytorch_model.bin"
        if not cfg_path.is_file():
            raise ModelUnavailable("diarization", f"Не найден config.yaml диаризации: {cfg_path}")
        if not seg_ckpt.is_file():
            raise ModelUnavailable("diarization", f"Не найдены веса сегментации: {seg_ckpt}")
        try:
            import yaml
            from pyannote.audio import Model
            from pyannote.audio.pipelines import SpeakerDiarization
        except ImportError as exc:
            raise ModelUnavailable("diarization", f"pyannote.audio не установлен: {exc}")
        cfg = yaml.safe_load(cfg_path.read_text())
        pparams = dict(cfg["pipeline"]["params"])
        segmentation = Model.from_pretrained(str(seg_ckpt))
        if segmentation is None:
            raise ModelUnavailable("diarization", "pyannote не смог загрузить модель сегментации")
        # Reuse the loaded embedding Inference's model when available: one version everywhere.
        emb_slot = self.slots["embedding"]
        embedding = emb_slot.obj.model if emb_slot.status == "ready" else self._embedding_model()
        pipeline = SpeakerDiarization(
            segmentation=segmentation,
            embedding=embedding,
            clustering=pparams.get("clustering", "AgglomerativeClustering"),
            embedding_exclude_overlap=pparams.get("embedding_exclude_overlap", True),
            embedding_batch_size=pparams.get("embedding_batch_size", 32),
            segmentation_batch_size=pparams.get("segmentation_batch_size", 32),
        )
        pipeline.instantiate(cfg["params"])
        pipeline.to(self.torch_device())
        return pipeline


# One heavy CPU job at a time across the process: full processing waits for it,
# live preview only tries it (and reports "waiting" when busy).
compute_lock = threading.Lock()

_registry: ModelRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ModelRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ModelRegistry(get_settings())
        return _registry


def set_registry(registry: ModelRegistry | None) -> None:
    """Tests inject a registry; production never calls this."""
    global _registry
    with _registry_lock:
        _registry = registry


def model_paths_exist(settings: Settings) -> dict[str, bool]:
    return {
        "asr": (Path(settings.resolved_asr_path) / "model.bin").is_file(),
        "diarization": settings.resolved_diarization_config.is_file()
        and (segmentation_path(settings) / "pytorch_model.bin").is_file(),
        "embedding": embedding_checkpoint(settings).is_file(),
    }
