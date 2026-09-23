"""Application configuration from environment variables.

Every threshold, model path and resource limit is configurable; non-secret
defaults are mirrored in ``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    # --- core ---
    database_url: str = "postgresql+psycopg://darai:darai@localhost:5432/darai"
    data_dir: Path = Path("./data")
    log_level: str = "INFO"
    run_migrations_on_start: bool = True

    # --- auth ---
    session_ttl_hours: int = Field(12, ge=1, le=24 * 30)
    cookie_secure: bool = False
    admin_login: str | None = None
    admin_password: str | None = None

    # --- uploads ---
    max_upload_mb: int = Field(500, ge=1)
    max_audio_seconds: int = Field(3 * 3600, ge=1)
    ffmpeg_timeout_seconds: int = Field(900, ge=5)

    # --- live recording ---
    live_max_chunk_bytes: int = Field(5 * 1024 * 1024, ge=1024)
    live_idle_timeout_seconds: int = Field(120, ge=10)
    live_poll_after_ms: int = Field(2000, ge=250)
    # Preview coalescing is time based: the worker looks at new bytes at most once per interval;
    # whether there is enough speech for ASR is decided by the AI (LIVE_PREVIEW_MIN_NEW_SECONDS).
    live_preview_check_interval_ms: int = Field(2000, ge=50)
    live_preview_min_new_bytes: int = Field(1, ge=1)
    live_preview_retry_max_ms: int = Field(30000, ge=100)  # cap for AI retry_after_ms
    live_preview_error_backoff_ms: int = Field(10000, ge=0)  # after preview_status=unavailable
    live_preview_enabled: bool = True

    # --- jobs ---
    max_concurrent_jobs: int = Field(1, ge=1, le=4)
    preload_models: bool = False

    # --- models (local paths only; nothing is downloaded at runtime) ---
    models_dir: Path = Path("/models")
    asr_model_path: Path | None = None  # default: {models_dir}/faster-whisper-large-v3
    asr_model_id: str = "faster-whisper-large-v3"
    asr_device: str = "auto"  # auto | cpu | cuda
    asr_compute_type: str = "default"  # int8 | float16 | int8_float16 | default
    asr_profile: Literal["standard", "refined"] = "refined"
    asr_language: str | None = None  # None/empty => autodetect
    asr_beam_size: int = Field(5, ge=1, le=10)
    asr_cpu_threads: int = Field(0, ge=0)

    diarization_config_path: Path | None = None  # default: {models_dir}/pyannote/speaker-diarization-3.1/config.yaml
    diarization_model_id: str = "pyannote/speaker-diarization-3.1"
    embedding_model_path: Path | None = None  # default: {models_dir}/pyannote/wespeaker-voxceleb-resnet34-LM
    segmentation_model_path: Path | None = None  # default: {models_dir}/pyannote/segmentation-3.0
    torch_device: str = "cpu"  # cpu | cuda | mps

    # --- voice identification ---
    voice_model_id: str = "pyannote/wespeaker-voxceleb-resnet34-LM"
    voice_model_revision: str = "local"
    voice_embedding_dim: int = 256
    voice_min_segment_seconds: float = Field(1.5, gt=0)
    voice_match_threshold: float = Field(0.55, ge=-1, le=1)
    voice_match_margin: float = Field(0.08, ge=0, le=2)
    voice_enroll_min_seconds: float = Field(15.0, gt=0)
    voice_enroll_chunk_seconds: float = Field(3.0, gt=0)
    voice_enroll_consistency_threshold: float = Field(0.5, ge=-1, le=1)
    voice_max_segments_per_speaker: int = Field(30, ge=1)

    # --- alignment ---
    align_max_gap_seconds: float = Field(1.0, ge=0)
    align_ambiguity_ratio: float = Field(0.6, gt=0, le=1)

    # --- LLM (local OpenAI-compatible endpoint only) ---
    llm_base_url: str = "http://ollama:11434/v1"
    llm_model: str = ""
    llm_api_key: str | None = None
    llm_timeout_seconds: float = Field(180.0, gt=0)
    llm_temperature: float = 0.0
    llm_window_utterances: int = Field(40, ge=7)
    llm_window_overlap: int = Field(6, ge=3)
    llm_summary_max_chars: int = Field(24000, ge=1000)
    llm_allowed_hosts: str = ""  # extra comma-separated hostnames considered local
    task_low_confidence: float = Field(0.6, ge=0, le=1)

    # --- export ---
    soffice_path: str = "soffice"
    soffice_timeout_seconds: int = Field(120, ge=5)
    export_font: str = "DejaVu Sans"

    # --- notifications ---
    reminder_interval_seconds: int = Field(60, ge=5)
    reminder_lead_hours: int = Field(24, ge=1)
    reminders_enabled: bool = True

    @field_validator("asr_language", mode="before")
    @classmethod
    def _empty_language(cls, v):
        return v or None

    # resolved paths -----------------------------------------------------
    @property
    def resolved_asr_path(self) -> Path:
        return self.asr_model_path or self.models_dir / "faster-whisper-large-v3"

    @property
    def resolved_diarization_config(self) -> Path:
        return self.diarization_config_path or (
            self.models_dir / "pyannote" / "speaker-diarization-3.1" / "config.yaml"
        )

    @property
    def resolved_embedding_path(self) -> Path:
        return self.embedding_model_path or (
            self.models_dir / "pyannote" / "wespeaker-voxceleb-resnet34-LM"
        )

    @property
    def resolved_segmentation_path(self) -> Path:
        return self.segmentation_model_path or self.models_dir / "pyannote" / "segmentation-3.0"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def extra_llm_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.llm_allowed_hosts.split(",") if h.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
