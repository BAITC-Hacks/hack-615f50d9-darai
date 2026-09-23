"""Alignment of ASR words to diarization turns and overlap detection."""

from app.align import Turn, Word, align, assign_words
from app.diarize import overlap_regions


def W(s, e, t):
    return Word(s, e, t)


def test_word_goes_to_max_overlap_speaker():
    turns = [Turn(0, 1.0, "A"), Turn(0.9, 3.0, "B")]
    aw = assign_words([W(0.8, 1.6, " слово")], turns, [], ambiguity_ratio=0.6)[0]
    assert aw.label == "B" and aw.reasons == set()  # 0.7 s vs 0.2 s


def test_close_runner_up_is_ambiguous_not_silent_guess():
    turns = [Turn(0, 1.0, "A"), Turn(1.0, 2.0, "B")]
    aw = assign_words([W(0.55, 1.4, " x")], turns, [], ambiguity_ratio=0.6)[0]
    assert aw.label == "A" and "ambiguous_speaker" in aw.reasons  # 0.45 s vs 0.40 s


def test_no_overlap_uses_nearest_within_tolerance_and_flags():
    turns = [Turn(0, 1.0, "A")]
    near = assign_words([W(1.2, 1.4, " a")], turns, [])[0]
    far = assign_words([W(5.0, 5.4, " b")], turns, [])[0]
    assert near.label == "A" and "no_speaker_overlap" in near.reasons
    assert far.label is None and "no_speaker_overlap" in far.reasons


def test_overlapped_speech_flag():
    turns = [Turn(0, 2, "A"), Turn(1, 3, "B")]
    ov = overlap_regions(turns)
    aw = assign_words([W(1.2, 1.5, " x")], turns, ov)[0]
    assert "overlapped_speech" in aw.reasons


def test_utterances_split_on_speaker_change_and_gap():
    turns = [Turn(0, 2, "A"), Turn(2, 4, "B"), Turn(6, 8, "B")]
    words = [W(0.1, 0.5, " Привет"), W(0.6, 1.0, " всем."), W(2.1, 2.5, " Сәлем"),
             W(6.2, 6.6, " Жарайды")]
    utts = align(words, turns, overlap_regions(turns), max_gap=1.0)
    assert [(u.speaker_label, u.text) for u in utts] == [
        ("A", "Привет всем."), ("B", "Сәлем"), ("B", "Жарайды")]
    assert not any(u.uncertain for u in utts)


def test_overlap_regions_sweep():
    turns = [Turn(0, 5, "A"), Turn(3, 6, "B"), Turn(5.5, 7, "A"), Turn(8, 9, "A"), Turn(8.5, 8.7, "A")]
    # same-speaker overlap (8.5-8.7) is not overlapped speech
    assert overlap_regions(turns) == [(3, 5), (5.5, 6)]


def test_touching_turns_are_not_overlap():
    assert overlap_regions([Turn(0, 1, "A"), Turn(1, 2, "B")]) == []
