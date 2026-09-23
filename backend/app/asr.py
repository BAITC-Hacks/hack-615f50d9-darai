"""faster-whisper transcription with word timestamps.

task=transcribe, language from ASR_LANGUAGE (None => autodetect).

Measured on fixtures (docs/AI_SETUP.md): faster-whisper's own ``multilingual``
switch re-detects language per 30 s window, not per utterance, so in a RU/KZ
meeting Kazakh turns came out as Russian transliteration. ``refine_languages``
therefore re-detects the language of every VAD segment, restricted to
ASR_ALLOWED_LANGUAGES, and re-decodes segments whose language differs.
Autodetect still does not guarantee quality of code-switching inside one
utterance.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .align import Word


@dataclass
class AsrSegment:
    start: float
    end: float
    text: str
    language: str | None
    words: list[Word] = field(default_factory=list)
    no_speech_prob: float | None = None


@dataclass
class AsrResult:
    segments: list[AsrSegment]
    language: str | None               # file-level detection
    language_probability: float | None

    @property
    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    @property
    def languages(self) -> list[str]:
        """Languages by amount of recognized words, most frequent first."""
        counts: Counter[str] = Counter()
        for s in self.segments:
            if s.language:
                counts[s.language] += max(1, len(s.words))
        return [lang for lang, _ in counts.most_common()]

    def language_at(self, t: float) -> str | None:
        for s in self.segments:
            if s.start <= t <= s.end:
                return s.language
        return self.language


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


SAMPLE_RATE = 16000


def transcribe(model, samples: np.ndarray, *, language: str | None = None, beam_size: int = 5,
               multilingual: bool = True, vad_filter: bool = True,
               is_cancelled=None, allowed_languages: list[str] | None = None,
               min_language_prob: float = 0.5,
               hallucination_silence_threshold: float | None = None,
               profile: str = "refined") -> AsrResult:
    """``samples``: float32 mono 16 kHz. Blocking; call outside the event loop."""
    if profile not in ("standard", "refined"):
        raise ValueError("Unknown ASR profile")
    extra = {}
    if hallucination_silence_threshold:
        extra["hallucination_silence_threshold"] = hallucination_silence_threshold
    segments_iter, info = model.transcribe(
        samples,
        task="transcribe",
        language=language,
        multilingual=profile == "refined" and multilingual and language is None,
        beam_size=beam_size,
        word_timestamps=True,
        vad_filter=vad_filter,
        condition_on_previous_text=profile == "standard",  # standard = adilet whole-file decoding
        **extra,
    )
    segments: list[AsrSegment] = []
    file_lang = getattr(info, "language", None)
    for seg in segments_iter:  # generator: decoding happens here
        if is_cancelled is not None and is_cancelled():
            break
        words = [Word(float(w.start), float(w.end), w.word) for w in (seg.words or [])
                 if w.word and w.word.strip() and w.end > w.start]
        seg_lang = getattr(seg, "language", None) or file_lang
        segments.append(AsrSegment(float(seg.start), float(seg.end), seg.text.strip(), seg_lang, words,
                                   getattr(seg, "no_speech_prob", None)))
    result = AsrResult(segments=segments, language=file_lang,
                       language_probability=getattr(info, "language_probability", None))
    if profile == "refined" and multilingual and language is None and allowed_languages:
        refine_languages(model, samples, result, allowed_languages, beam_size=beam_size,
                         min_prob=min_language_prob, is_cancelled=is_cancelled)
    return result


def pick_language(all_probs: list[tuple[str, float]], allowed: list[str]) -> tuple[str | None, float]:
    """Best language among ``allowed``, renormalized over them."""
    probs = {lang: p for lang, p in all_probs if lang in allowed}
    total = sum(probs.values())
    if not probs or total <= 0:
        return None, 0.0
    lang = max(probs, key=probs.get)
    return lang, probs[lang] / total


def refine_languages(model, samples: np.ndarray, result: AsrResult, allowed: list[str], *,
                     beam_size: int = 5, min_prob: float = 0.5, min_seconds: float = 1.0,
                     is_cancelled=None) -> int:
    """Per-segment language re-detection + re-decode. Returns number of re-decoded segments."""
    changed = 0
    for i, seg in enumerate(result.segments):
        if is_cancelled is not None and is_cancelled():
            break
        if seg.end - seg.start < min_seconds:
            continue
        a, b = int(seg.start * SAMPLE_RATE), int(seg.end * SAMPLE_RATE)
        clip = samples[max(0, a - 1600):min(len(samples), b + 1600)]  # 0.1 s padding
        offset = max(0, a - 1600) / SAMPLE_RATE
        _, _, all_probs = model.detect_language(clip)
        lang, prob = pick_language(all_probs, allowed)
        if lang is None or lang == seg.language or prob < min_prob:
            continue
        sub_iter, _ = model.transcribe(clip, task="transcribe", language=lang, beam_size=beam_size,
                                       word_timestamps=True, vad_filter=False,
                                       condition_on_previous_text=False)
        subs = list(sub_iter)
        words = [Word(float(w.start) + offset, float(w.end) + offset, w.word)
                 for sub in subs for w in (sub.words or []) if w.word and w.word.strip() and w.end > w.start]
        text = " ".join(sub.text.strip() for sub in subs).strip()
        if not words or not text:
            continue
        result.segments[i] = AsrSegment(seg.start, seg.end, text, lang, words)
        changed += 1
    return changed
