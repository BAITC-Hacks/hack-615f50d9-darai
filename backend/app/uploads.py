"""Safe storage of uploaded files: server-chosen names, size limit, ffprobe check."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from fastapi import UploadFile

from .audio import AudioError, probe_duration
from .config import get_settings
from .errors import ApiError

_EXT = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
_CHUNK = 1024 * 1024


def safe_suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if _EXT.match(suffix) else ".bin"


def display_name(filename: str | None) -> str:
    name = Path(filename or "recording").name
    return re.sub(r"[\x00-\x1f]", "", name)[:255] or "recording"


def save_upload(upload: UploadFile, dest: Path) -> int:
    limit = get_settings().max_upload_mb * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    upload.file.seek(0)
    with open(dest, "wb") as out:
        while True:
            chunk = upload.file.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise ApiError(413, "FILE_TOO_LARGE", f"Файл больше {get_settings().max_upload_mb} МБ")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise ApiError(422, "AUDIO_INVALID", "Пустой файл")
    return size


def check_audio(path: Path) -> float | None:
    """ffprobe the file; returns duration (None if the container has no header)."""
    settings = get_settings()
    try:
        duration = probe_duration(path)
    except AudioError as exc:
        status = 422 if exc.code == "AUDIO_INVALID" else 503
        raise ApiError(status, exc.code, exc.message)
    if duration > settings.max_audio_seconds:
        raise ApiError(422, "AUDIO_TOO_LONG",
                       f"Длительность {duration:.0f} с превышает лимит {settings.max_audio_seconds} с",
                       {"duration_seconds": round(duration, 1), "max_seconds": settings.max_audio_seconds})
    return duration if duration > 0 else None


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
