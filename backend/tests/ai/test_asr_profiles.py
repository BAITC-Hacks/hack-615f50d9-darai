from types import SimpleNamespace
import numpy as np
import pytest
from app.asr import transcribe


class Model:
    def __init__(self):
        self.calls = []

    def transcribe(self, samples, **kwargs):
        self.calls.append(kwargs)
        w = SimpleNamespace(start=0., end=1., word=" Спасибо за внимание!")
        seg = SimpleNamespace(start=0., end=1., text=w.word, words=[w], no_speech_prob=.1)
        return iter([seg]), SimpleNamespace(language=kwargs.get("language") or "ru", language_probability=.99)

    def detect_language(self, clip):
        raise AssertionError("Standard/forced-language must not re-detect segments")


@pytest.mark.parametrize("language", [None, "ru", "kk"])
def test_standard_keeps_adilet_context_and_word_timestamps(language):
    model = Model()
    result = transcribe(model, np.zeros(16000, dtype=np.float32), profile="standard",
                        language=language, allowed_languages=["ru", "kk"])
    assert result.words[0].text.strip() == "Спасибо за внимание!"
    assert model.calls[0]["condition_on_previous_text"] is True
    assert model.calls[0]["multilingual"] is False
    assert model.calls[0]["language"] == language
    assert model.calls[0]["word_timestamps"] is True


def test_refined_forced_language_does_not_override_user_choice():
    model = Model()
    transcribe(model, np.zeros(16000, dtype=np.float32), profile="refined", language="kk",
               allowed_languages=["ru", "kk"])
    assert model.calls[0]["condition_on_previous_text"] is False
    assert model.calls[0]["language"] == "kk"


def test_invalid_profile_rejected():
    with pytest.raises(ValueError):
        transcribe(Model(), np.zeros(16000), profile="missing")
