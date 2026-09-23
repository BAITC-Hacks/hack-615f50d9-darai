"""FFmpeg helpers: probe uploads and normalize to 16 kHz mono PCM WAV."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


class AudioError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def probe_duration(path: Path, timeout: int = 60) -> float:
    """Duration in seconds of the first audio stream; AudioError if none."""
    if not tool_available("ffprobe"):
        raise AudioError("FFMPEG_FAILED", "ffprobe не установлен")
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index,duration:format=duration", "-of", "json", str(path)],
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise AudioError("FFMPEG_FAILED", "ffprobe превысил таймаут")
    if proc.returncode != 0:
        raise AudioError("AUDIO_INVALID", "Файл не распознан как аудио или видео")
    try:
        info = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError:
        raise AudioError("AUDIO_INVALID", "Файл не распознан как аудио или видео")
    streams = info.get("streams") or []
    if not streams:
        raise AudioError("AUDIO_INVALID", "В файле нет аудиодорожки")
    for candidate in (streams[0].get("duration"), (info.get("format") or {}).get("duration")):
        try:
            if candidate is not None and float(candidate) > 0:
                return float(candidate)
        except ValueError:
            continue
    # Some containers (e.g. MediaRecorder webm) carry no duration header.
    return -1.0


def normalize(src: Path, dst: Path, timeout: int) -> None:
    if not tool_available("ffmpeg"):
        raise AudioError("FFMPEG_FAILED", "ffmpeg не установлен")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.wav")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", str(tmp)],
            capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        raise AudioError("FFMPEG_FAILED", "FFmpeg превысил таймаут")
    if proc.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise AudioError("FFMPEG_FAILED", "FFmpeg не смог преобразовать файл")
    tmp.replace(dst)


def load_wav(path: Path) -> np.ndarray:
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if sr != SAMPLE_RATE:
        raise AudioError("FFMPEG_FAILED", f"Ожидалась частота {SAMPLE_RATE} Гц")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data


def clipping_ratio(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.mean(np.abs(samples) >= 0.99))
