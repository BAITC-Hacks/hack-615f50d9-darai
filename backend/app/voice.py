"""Voice profiles: enrollment, per-speaker vectors and matching.

Enrollment and matching share one preprocessing path: normalized 16 kHz mono
audio -> speech regions -> chunks of VOICE_ENROLL_CHUNK_SECONDS (each at least
VOICE_MIN_SEGMENT_SECONDS) -> WeSpeaker embedding per chunk -> L2 normalize ->
mean -> L2 normalize.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from .align import Turn

# ---------------------------------------------------------------- math utils


def l2_normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64).reshape(-1)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n == 0.0:
        raise ValueError("zero or non-finite vector")
    return v / n


def mean_embedding(vectors: list[np.ndarray]) -> np.ndarray:
    if not vectors:
        raise ValueError("no vectors")
    normalized = [l2_normalize(v) for v in vectors]
    return l2_normalize(np.mean(normalized, axis=0))


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(l2_normalize(a), l2_normalize(b)))


# ---------------------------------------------------------------- segments


def subtract_intervals(start: float, end: float, holes: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pieces = [(start, end)]
    for h0, h1 in holes:
        nxt = []
        for p0, p1 in pieces:
            if h1 <= p0 or h0 >= p1:
                nxt.append((p0, p1))
                continue
            if h0 > p0:
                nxt.append((p0, h0))
            if h1 < p1:
                nxt.append((h1, p1))
        pieces = nxt
    return pieces


def clean_segments(turns: list[Turn], overlaps: list[tuple[float, float]],
                   min_seconds: float) -> dict[str, list[tuple[float, float]]]:
    """Per speaker: turn pieces without overlapped speech, at least ``min_seconds`` long."""
    result: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        result.setdefault(t.label, [])
        for s, e in subtract_intervals(t.start, t.end, overlaps):
            if e - s >= min_seconds:
                result[t.label].append((s, e))
    return result


def chunk_regions(regions: list[tuple[float, float]], chunk_seconds: float,
                  min_seconds: float, max_chunks: int | None = None) -> list[tuple[float, float]]:
    chunks: list[tuple[float, float]] = []
    for s, e in regions:
        t = s
        while e - t >= min_seconds:
            c_end = min(e, t + chunk_seconds)
            # fold a too-short tail into the current chunk
            if e - c_end < min_seconds:
                c_end = e
            chunks.append((t, c_end))
            t = c_end
    if max_chunks is not None and len(chunks) > max_chunks:
        # keep the longest chunks: they are the most reliable
        chunks = sorted(sorted(chunks, key=lambda c: c[1] - c[0], reverse=True)[:max_chunks])
    return chunks


def energy_speech_regions(samples: np.ndarray, sample_rate: int, frame_seconds: float = 0.03,
                          min_region: float = 0.3, merge_gap: float = 0.3) -> list[tuple[float, float]]:
    """Simple energy VAD used for enrollment samples (single speaker, no diarization)."""
    frame = max(1, int(frame_seconds * sample_rate))
    n = len(samples) // frame
    if n == 0:
        return []
    frames = samples[: n * frame].reshape(n, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    threshold = max(float(np.percentile(db, 10)) + 12.0, -50.0)
    speech = db > threshold
    regions: list[tuple[float, float]] = []
    start = None
    for i, flag in enumerate(speech):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            regions.append((start * frame_seconds, i * frame_seconds))
            start = None
    if start is not None:
        regions.append((start * frame_seconds, n * frame_seconds))
    merged: list[tuple[float, float]] = []
    for s, e in regions:
        if merged and s - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= min_region]


# ---------------------------------------------------------------- matching


@dataclass
class MatchResult:
    proposed_employee_id: uuid.UUID | None
    similarity: float | None
    second_similarity: float | None
    review_required: bool
    review_reasons: list[str] = field(default_factory=list)


_FORBIDDEN = 1e6


def match_speakers(speaker_vectors: dict[str, np.ndarray | None],
                   candidates: dict[uuid.UUID, np.ndarray],
                   threshold: float, margin: float) -> dict[str, MatchResult]:
    """Propose an employee per speaker, allowing "unknown".

    Only a speaker's best candidate can be proposed, and only when it passes the
    similarity threshold and (with 2+ candidates) the margin over the runner-up.
    The Hungarian step then resolves several speakers competing for the same
    employee: each speaker also has its own dummy "unknown" column with zero
    cost, so declining is always feasible and nobody gets their second choice.
    """
    results: dict[str, MatchResult] = {}
    labels = sorted(speaker_vectors)
    cand_ids = list(candidates)
    cand_matrix = (np.stack([l2_normalize(candidates[c]) for c in cand_ids]) if cand_ids else None)

    active: list[str] = []
    sims: dict[str, np.ndarray] = {}
    for label in labels:
        vec = speaker_vectors[label]
        if vec is None:
            results[label] = MatchResult(None, None, None, True, ["no_clean_speech"])
            continue
        if cand_matrix is None:
            results[label] = MatchResult(None, None, None, True, ["no_candidates"])
            continue
        s = cand_matrix @ l2_normalize(vec)
        sims[label] = s
        active.append(label)

    if not active:
        return results

    n_l, n_c = len(active), len(cand_ids)
    cost = np.full((n_l, n_c + n_l), _FORBIDDEN)
    pre_reasons: dict[str, list[str]] = {}
    for i, label in enumerate(active):
        s = sims[label]
        order = np.argsort(-s)
        best = int(order[0])
        best_sim = float(s[best])
        second = float(s[order[1]]) if n_c > 1 else None
        reasons: list[str] = []
        if best_sim < threshold:
            reasons.append("below_threshold")
        if second is not None and best_sim - second < margin:
            reasons.append("low_margin")
        pre_reasons[label] = reasons
        if not reasons:
            cost[i, best] = -best_sim
        cost[i, n_c + i] = 0.0  # this speaker's own "unknown" option

    rows, cols = linear_sum_assignment(cost)
    chosen = {active[r]: c for r, c in zip(rows, cols)}

    # Several speakers confidently matching one employee usually means the
    # diarization split one person into clusters: the winner is kept as a
    # proposal but goes to manual review (manual mapping may be many-to-one).
    confident_best: dict[int, int] = {}
    for label in active:
        if not pre_reasons[label]:
            b = int(np.argmax(sims[label]))
            confident_best[b] = confident_best.get(b, 0) + 1

    for i, label in enumerate(active):
        s = sims[label]
        order = np.argsort(-s)
        best = int(order[0])
        best_sim = float(s[best])
        second = float(s[order[1]]) if n_c > 1 else None
        second_r = None if second is None else round(second, 4)
        col = chosen[label]
        reasons = list(pre_reasons[label])
        split = not reasons and confident_best.get(best, 0) > 1
        if col < n_c and cost[i, col] < _FORBIDDEN:
            results[label] = MatchResult(cand_ids[col], round(best_sim, 4), second_r, split,
                                         ["split_cluster_suspected"] if split else [])
        else:
            if not reasons:
                reasons.append("candidate_taken")
            if split:
                reasons.append("split_cluster_suspected")
            results[label] = MatchResult(None, round(best_sim, 4), second_r, True, reasons)
    return results


# ---------------------------------------------------------------- embeddings (models)


def embed_regions(inference, waveform, sample_rate: int, regions: list[tuple[float, float]],
                  chunk_seconds: float, min_seconds: float, max_chunks: int) -> list[np.ndarray]:
    """Embed each chunk of the given regions with the shared WeSpeaker inference."""
    from pyannote.core import Segment  # heavy import, only with models installed

    chunks = chunk_regions(regions, chunk_seconds, min_seconds, max_chunks)
    audio = {"waveform": waveform, "sample_rate": sample_rate}
    vectors: list[np.ndarray] = []
    for s, e in chunks:
        emb = inference.crop(audio, Segment(s, e))
        v = np.asarray(emb, dtype=np.float64).reshape(-1)
        if np.all(np.isfinite(v)) and np.linalg.norm(v) > 0:
            vectors.append(l2_normalize(v))
    return vectors


def speaker_vectors(inference, waveform, sample_rate: int, clean: dict[str, list[tuple[float, float]]],
                    chunk_seconds: float, min_seconds: float, max_chunks: int,
                    ) -> dict[str, np.ndarray | None]:
    """Per-speaker profile vector from clean segments; None when there is not enough."""
    out: dict[str, np.ndarray | None] = {}
    for label, regions in clean.items():
        vectors = embed_regions(inference, waveform, sample_rate, regions, chunk_seconds,
                                min_seconds, max_chunks) if regions else []
        out[label] = mean_embedding(vectors) if vectors else None
    return out


# ---------------------------------------------------------------- enrollment


@dataclass
class EnrollQuality:
    speech_seconds: float
    clipping_ratio: float
    reject_reasons: list[str]
    review_reasons: list[str]


def enrollment_quality(samples: np.ndarray, sample_rate: int, regions: list[tuple[float, float]],
                       min_speech_seconds: float, clip_review: float, clip_reject: float) -> EnrollQuality:
    speech = sum(e - s for s, e in regions)
    clip = float(np.mean(np.abs(samples) >= 0.99)) if samples.size else 0.0
    reject: list[str] = []
    review: list[str] = []
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if not regions or peak < 1e-3:
        reject.append("silence")
    elif speech < min_speech_seconds:
        reject.append("too_short")
    if clip >= clip_reject:
        reject.append("clipping")
    elif clip >= clip_review:
        review.append("clipping")
    return EnrollQuality(round(speech, 2), clip, reject, review)


def enroll_vector(vectors: list[np.ndarray], consistency_threshold: float) -> tuple[np.ndarray, float, list[str]]:
    """Mean profile + min cosine of a chunk to the mean (low => mixed voices/noise)."""
    profile = mean_embedding(vectors)
    consistency = min(float(np.dot(l2_normalize(v), profile)) for v in vectors)
    reasons = ["inconsistent_voice"] if consistency < consistency_threshold else []
    return profile, round(consistency, 4), reasons
