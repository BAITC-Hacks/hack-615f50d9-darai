"""Voice vectors, clean-speech selection and matching with an "unknown" option."""

import uuid

import numpy as np
import pytest

from app.align import Turn
from app.voice import (
    chunk_regions,
    clean_segments,
    energy_speech_regions,
    enroll_vector,
    enrollment_quality,
    l2_normalize,
    match_speakers,
    mean_embedding,
)

TH, MARGIN = 0.55, 0.08
A, B, C = (uuid.UUID(int=i) for i in (1, 2, 3))


def unit(*xs):
    return l2_normalize(np.array(xs, dtype=float))


def vec_with_cos(base: np.ndarray, cos: float) -> np.ndarray:
    """Vector with exact cosine ``cos`` to ``base`` (3-D, orthogonal part along a fixed axis)."""
    base = l2_normalize(base)
    ortho = np.array([0.0, 0.0, 1.0]) - base * base[2]
    ortho = l2_normalize(ortho)
    return cos * base + np.sqrt(1 - cos**2) * ortho


def test_mean_embedding_normalizes_before_and_after():
    v = mean_embedding([np.array([10.0, 0, 0]), np.array([0, 0.1, 0])])
    assert np.allclose(v, unit(1, 1, 0))
    with pytest.raises(ValueError):
        l2_normalize(np.zeros(3))


def test_clean_segments_exclude_overlap_and_short_pieces():
    turns = [Turn(0, 4, "A"), Turn(3, 5, "B")]
    clean = clean_segments(turns, [(3, 4)], 1.5)
    assert clean == {"A": [(0, 3)], "B": [(4, 5)][:0]}  # B's 1 s remainder < 1.5 s


def test_chunk_regions_min_length_and_tail_fold():
    assert chunk_regions([(0, 7.0)], 3.0, 1.5) == [(0, 3.0), (3.0, 7.0)]
    assert chunk_regions([(0, 1.2)], 3.0, 1.5) == []
    top = chunk_regions([(0, 2), (10, 13), (20, 21.6)], 3.0, 1.5, max_chunks=2)
    assert top == [(0, 2), (10, 13)]


def test_known_speakers_matched():
    a, b = unit(1, 0, 0), unit(0, 1, 0)
    res = match_speakers({"S0": vec_with_cos(a, 0.8), "S1": vec_with_cos(b, 0.7)}, {A: a, B: b}, TH, MARGIN)
    assert res["S0"].proposed_employee_id == A and not res["S0"].review_required
    assert res["S1"].proposed_employee_id == B
    assert res["S0"].second_similarity is not None


def test_unregistered_voice_is_unknown():
    a, b = unit(1, 0, 0), unit(0, 1, 0)
    res = match_speakers({"S0": unit(0, 0, 1)}, {A: a, B: b}, TH, MARGIN)
    assert res["S0"].proposed_employee_id is None and "below_threshold" in res["S0"].review_reasons


def test_low_margin_declines():
    a, b = unit(1, 0, 0), unit(0.9, 0.44, 0)
    s = l2_normalize(a + b)
    res = match_speakers({"S0": s}, {A: a, B: b}, TH, MARGIN)
    assert res["S0"].proposed_employee_id is None and "low_margin" in res["S0"].review_reasons


def test_single_candidate_uses_threshold_only_and_no_runner_up():
    a = unit(1, 0, 0)
    ok = match_speakers({"S0": vec_with_cos(a, 0.6)}, {A: a}, TH, MARGIN)["S0"]
    no = match_speakers({"S0": vec_with_cos(a, 0.5)}, {A: a}, TH, MARGIN)["S0"]
    assert ok.proposed_employee_id == A and ok.second_similarity is None
    assert no.proposed_employee_id is None and no.review_reasons == ["below_threshold"]


def test_second_best_is_never_forced():
    """S1's best is A (taken by S0 with higher sim); S1 must NOT fall back to B."""
    a, b = unit(1, 0, 0), unit(0, 1, 0)
    s0 = vec_with_cos(a, 0.95)
    s1 = l2_normalize(0.75 * a + 0.3 * b + 0.3 * np.array([0, 0, 1.0]))
    res = match_speakers({"S0": s0, "S1": s1}, {A: a, B: b}, TH, MARGIN)
    assert res["S0"].proposed_employee_id == A
    assert res["S1"].proposed_employee_id is None
    assert "candidate_taken" in res["S1"].review_reasons
    assert "split_cluster_suspected" in res["S1"].review_reasons
    assert res["S0"].review_required and res["S0"].review_reasons == ["split_cluster_suspected"]


def test_no_clean_speech_and_no_candidates():
    a = unit(1, 0, 0)
    assert match_speakers({"S0": None}, {A: a}, TH, MARGIN)["S0"].review_reasons == ["no_clean_speech"]
    assert match_speakers({"S0": a}, {}, TH, MARGIN)["S0"].review_reasons == ["no_candidates"]


def test_enrollment_quality_rejections():
    sr = 16000
    silence = np.zeros(sr * 5, dtype=np.float32)
    q = enrollment_quality(silence, sr, energy_speech_regions(silence, sr), 15, 0.001, 0.02)
    assert q.reject_reasons == ["silence"]

    rng = np.random.default_rng(0)
    t = np.arange(sr * 4) / sr
    tone = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + 0.1 * rng.standard_normal(t.size))).astype(np.float32)
    short = np.concatenate([np.zeros(sr, np.float32), tone, np.zeros(sr, np.float32)])
    q = enrollment_quality(short, sr, energy_speech_regions(short, sr), 15, 0.001, 0.02)
    assert q.reject_reasons == ["too_short"] and 3.5 < q.speech_seconds < 4.5

    clipped = np.clip(short * 10, -1, 1)
    q = enrollment_quality(clipped, sr, energy_speech_regions(clipped, sr), 1, 0.001, 0.02)
    assert "clipping" in q.reject_reasons


def test_enroll_vector_flags_inconsistent_voice():
    a, b = unit(1, 0, 0), unit(0, 1, 0)
    _, cons, reasons = enroll_vector([a, a, vec_with_cos(a, 0.95)], 0.5)
    assert reasons == [] and cons > 0.9
    _, cons, reasons = enroll_vector([a, a, b, b], 0.8)
    assert reasons == ["inconsistent_voice"]
