"""pyannote speaker diarization -> turns + overlapped-speech regions.

Overlap regions are kept so alignment can flag words spoken over each other
and voice embeddings can skip them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .align import Turn


@dataclass
class DiarizationResult:
    turns: list[Turn]
    overlaps: list[tuple[float, float]]

    @property
    def labels(self) -> list[str]:
        return sorted({t.label for t in self.turns})

    def speech_seconds(self, label: str) -> float:
        return sum(t.end - t.start for t in self.turns if t.label == label)


def overlap_regions(turns: list[Turn]) -> list[tuple[float, float]]:
    """Intervals where 2+ different speakers are active (sweep line)."""
    events: list[tuple[float, int, str]] = []
    for t in turns:
        if t.end > t.start:
            events.append((t.start, 1, t.label))
            events.append((t.end, -1, t.label))
    # ends before starts at the same instant: touching turns are not an overlap
    events.sort(key=lambda e: (e[0], e[1]))
    active: dict[str, int] = {}
    out: list[tuple[float, float]] = []
    start: float | None = None
    for time, delta, label in events:
        active[label] = active.get(label, 0) + delta
        if active[label] == 0:
            del active[label]
        n = len(active)
        if n >= 2 and start is None:
            start = time
        elif n < 2 and start is not None:
            if time > start:
                out.append((start, time))
            start = None
    merged: list[tuple[float, float]] = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def diarize(pipeline, samples: np.ndarray, sample_rate: int, *, min_speakers: int | None = None,
            max_speakers: int | None = None) -> DiarizationResult:
    """``samples``: float32 mono. Blocking; call outside the event loop."""
    import torch

    waveform = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32)).unsqueeze(0)
    kwargs = {}
    if min_speakers:
        kwargs["min_speakers"] = min_speakers
    if max_speakers:
        kwargs["max_speakers"] = max_speakers
    annotation = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **kwargs)
    turns = [Turn(round(float(seg.start), 3), round(float(seg.end), 3), str(label))
             for seg, _, label in annotation.itertracks(yield_label=True) if seg.end > seg.start]
    turns.sort(key=lambda t: (t.start, t.end))
    return DiarizationResult(turns=turns, overlaps=overlap_regions(turns))
