"""Assign ASR words to diarization speakers and group them into utterances.

Each word gets the speaker whose turns overlap it most. Words with no overlap,
words inside overlapped speech and words with a close runner-up keep an
explicit uncertainty flag instead of a silent guess.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Turn:
    start: float
    end: float
    label: str


@dataclass
class AlignedWord:
    word: Word
    label: str | None
    reasons: set[str] = field(default_factory=set)


@dataclass
class AlignedUtterance:
    start: float
    end: float
    speaker_label: str | None
    text: str
    uncertain_reasons: list[str]

    @property
    def uncertain(self) -> bool:
        return bool(self.uncertain_reasons)


NEAREST_TOLERANCE_SECONDS = 0.5
MAX_UTTERANCE_SECONDS = 30.0
_SENTENCE_END = (".", "!", "?", "…")


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def assign_words(words: list[Word], turns: list[Turn], overlaps: list[tuple[float, float]],
                 ambiguity_ratio: float = 0.6) -> list[AlignedWord]:
    result: list[AlignedWord] = []
    for w in words:
        per_label: dict[str, float] = defaultdict(float)
        for t in turns:
            if t.end <= w.start or t.start >= w.end:
                continue
            per_label[t.label] += _overlap(w.start, w.end, t.start, t.end)
        reasons: set[str] = set()
        label: str | None
        ranked = sorted(per_label.items(), key=lambda kv: kv[1], reverse=True)
        if ranked and ranked[0][1] > 0:
            label = ranked[0][0]
            if len(ranked) > 1 and ranked[1][1] >= ambiguity_ratio * ranked[0][1]:
                reasons.add("ambiguous_speaker")
        else:
            label = _nearest_label(w, turns)
            reasons.add("no_speaker_overlap")
        mid = (w.start + w.end) / 2
        if any(o0 <= mid <= o1 for o0, o1 in overlaps):
            reasons.add("overlapped_speech")
        result.append(AlignedWord(w, label, reasons))
    return result


def _nearest_label(w: Word, turns: list[Turn]) -> str | None:
    best: tuple[float, str] | None = None
    for t in turns:
        gap = max(t.start - w.end, w.start - t.end, 0.0)
        if gap <= NEAREST_TOLERANCE_SECONDS and (best is None or gap < best[0]):
            best = (gap, t.label)
    return best[1] if best else None


def group_utterances(aligned: list[AlignedWord], max_gap: float = 1.0) -> list[AlignedUtterance]:
    utterances: list[AlignedUtterance] = []
    current: list[AlignedWord] = []

    def flush() -> None:
        if not current:
            return
        text = "".join(a.word.text for a in current).strip()
        if text:
            reasons = sorted(set().union(*(a.reasons for a in current)))
            utterances.append(AlignedUtterance(
                start=round(current[0].word.start, 3), end=round(current[-1].word.end, 3),
                speaker_label=current[0].label, text=text, uncertain_reasons=reasons,
            ))
        current.clear()

    for a in aligned:
        if current:
            prev = current[-1]
            too_long = (a.word.start - current[0].word.start) > MAX_UTTERANCE_SECONDS and \
                prev.word.text.strip().endswith(_SENTENCE_END)
            if a.label != prev.label or (a.word.start - prev.word.end) > max_gap or too_long:
                flush()
        current.append(a)
    flush()
    return utterances


def align(words: list[Word], turns: list[Turn], overlaps: list[tuple[float, float]],
          max_gap: float = 1.0, ambiguity_ratio: float = 0.6) -> list[AlignedUtterance]:
    return group_utterances(assign_words(words, turns, overlaps, ambiguity_ratio), max_gap)
