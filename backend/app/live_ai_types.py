"""Typed boundary for the live transcript preview. See docs/LIVE_AI_CONTRACT.md.

The AI layer keeps no state of its own: everything needed for the next
incremental call travels in ``LivePreviewResult.state`` and comes back as
``LivePreviewRequest.previous``. ``to_dict``/``from_dict`` allow the backend to
persist it as JSON (e.g. across a restart).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal

PreviewStatus = Literal["waiting", "processing", "ready", "unavailable"]


@dataclass(frozen=True)
class LivePreviewUtterance:
    id: str                 # stable within a session: "preview-0", ...
    start: float            # seconds from the start of the recording
    end: float
    speaker_label: str | None   # always None in preview (no live diarization)
    text: str
    is_final: bool          # True: this utterance will not change any more


@dataclass(frozen=True)
class PreviewWord:
    start: float
    end: float
    text: str               # as emitted by ASR, with its leading space


@dataclass(frozen=True)
class LivePreviewState:
    committed_until: float = 0.0          # audio before this point is final and never re-decoded
    next_id: int = 0                      # number of final utterances emitted so far
    final: tuple[LivePreviewUtterance, ...] = ()
    open_words: tuple[PreviewWord, ...] = ()   # committed words of the not-yet-closed utterance
    tentative: tuple[PreviewWord, ...] = ()    # live-edge words, re-decoded next time


@dataclass(frozen=True)
class LivePreviewResult:
    processed_until_seconds: float            # end of the audio actually transcribed so far
    utterances: list[LivePreviewUtterance]    # FULL snapshot, not a delta
    preview_status: PreviewStatus = "ready"
    preview_error: dict | None = None         # {"code", "message"}; technical, no content
    draft_tasks: list = field(default_factory=list)   # P1; always [] for now
    state: LivePreviewState = field(default_factory=LivePreviewState)
    decoded_seconds: float = 0.0              # decodable audio in the snapshot
    asr_seconds: float = 0.0                  # ASR wall time spent in this call

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LivePreviewResult":
        st = d.get("state") or {}
        utt = lambda x: LivePreviewUtterance(**x)  # noqa: E731
        word = lambda x: PreviewWord(**x)  # noqa: E731
        state = LivePreviewState(
            committed_until=float(st.get("committed_until", 0.0)), next_id=int(st.get("next_id", 0)),
            final=tuple(utt(x) for x in st.get("final", ())),
            open_words=tuple(word(x) for x in st.get("open_words", ())),
            tentative=tuple(word(x) for x in st.get("tentative", ())))
        return LivePreviewResult(
            processed_until_seconds=float(d.get("processed_until_seconds", 0.0)),
            utterances=[utt(x) for x in d.get("utterances", [])],
            preview_status=d.get("preview_status", "ready"), preview_error=d.get("preview_error"),
            draft_tasks=list(d.get("draft_tasks") or []), state=state,
            decoded_seconds=float(d.get("decoded_seconds", 0.0)), asr_seconds=float(d.get("asr_seconds", 0.0)))


def _never_cancelled() -> bool:
    return False


@dataclass(frozen=True)
class LivePreviewRequest:
    container_path: Path     # FIXED snapshot: copy of the first stable_bytes of the container,
                             # or a finished PCM WAV; never a file that is still being appended
    mime_type: str           # "audio/webm;codecs=opus", "audio/wav", ...
    stable_bytes: int
    work_dir: Path           # AI temp files; backend removes it with the session
    previous: LivePreviewResult | None = None
    is_cancelled: Callable[[], bool] = _never_cancelled
