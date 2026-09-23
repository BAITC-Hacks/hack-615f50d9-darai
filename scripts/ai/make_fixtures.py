#!/usr/bin/env python3
"""Generate SIMULATED speech fixtures with macOS TTS (voices Milena ru_RU, Aru kk_KZ).

These are synthetic voices, not real people: good for a reproducible pipeline
smoke run (ASR on RU/KK/mixed text, diarization of two distinct voices,
enrollment vs. meeting identity), NOT a substitute for real-meeting quality
evaluation. Output: backend/tests/fixtures/ai/*.wav + manifest.json.

Usage: python3 scripts/ai/make_fixtures.py   (macOS, needs `say` and `ffmpeg`)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "backend" / "tests" / "fixtures" / "ai"
SR = 16000
GAP = 0.5  # seconds of silence between turns

RU, KK = "Milena", "Aru"

ENROLL = {
    # Different text from any meeting fixture: enrollment and test must differ.
    "enroll_ru_milena": (RU, "Добрый день. Меня зовут Анна, я руковожу финансовым департаментом. "
                             "Сегодня я записываю образец голоса для системы протоколирования. "
                             "Утром я обычно просматриваю почту, затем провожу планёрку с командой. "
                             "По вторникам мы обсуждаем исполнение бюджета и закупки, "
                             "а по четвергам готовим материалы для правления. "
                             "Я люблю ясные формулировки и короткие совещания."),
    "enroll_kk_aru": (KK, "Сәлеметсіз бе. Менің атым Дана, мен экономист болып жұмыс істеймін. "
                          "Бүгін мен дауыс үлгісін жазып жатырмын. "
                          "Таңертең мен есептерді тексеремін, түстен кейін әріптестеріммен кездесемін. "
                          "Біздің бөлім бюджетті жоспарлаумен және талдаумен айналысады. "
                          "Демалыс күндері мен кітап оқығанды және тауға шыққанды ұнатамын."),
    "enroll_short": (RU, "Добрый день, это короткий образец."),
}

MEETING = [
    (RU, "ru", "Коллеги, начинаем совещание по бюджету на следующий квартал."),
    (KK, "kk", "Сәлеметсіздер ме. Мен алдын ала есепті дайындап қойдым."),
    (RU, "ru", "Дана, подготовьте, пожалуйста, финансовый отчёт до пятницы."),
    (KK, "mixed", "Жарайды, финансовый отчётты жұмаға дейін дайындаймын."),
    (RU, "ru", "Также нужно обновить презентацию для совета директоров."),
    (KK, "kk", "Презентацияны кім жасайды?"),
    (RU, "ru", "Этим займусь я сама, срок пока не определён."),
    (RU, "ru", "Всем спасибо, совещание окончено."),
]

KK_ONLY = [
    (KK, "kk", "Бүгінгі жиналыста біз жаңа жобаның жоспарын талқыладық."),
    (KK, "kk", "Ертеңге дейін барлық құжаттарды жинау керек."),
]
MIXED = [
    (KK, "mixed", "Коллеги, келесі аптада дедлайн бар, отчётты уақытында тапсыру керек."),
    (KK, "mixed", "Бюджет бойынша презентацияны понедельникке дейін жіберіңіздер."),
]
UNKNOWN_VOICE = [  # Milena pitched up: a voice nobody enrolled
    ("Milena+pitch", "ru", "Добрый день, я сегодня здесь как приглашённый эксперт по закупкам."),
    ("Milena+pitch", "ru", "Предлагаю вернуться к вопросу поставщиков на следующей неделе."),
]


def tts(voice: str, text: str, dst: Path) -> None:
    base, _, effect = voice.partition("+")
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "x.aiff"
        subprocess.run(["say", "-v", base, "-o", str(aiff), text], check=True)
        af = ["-af", "asetrate=22050*1.22,aresample=22050"] if effect == "pitch" else []
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(aiff), *af,
                        "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)], check=True)


def read_pcm(p: Path) -> bytes:
    with wave.open(str(p)) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        return w.readframes(w.getnframes())


def write_pcm(p: Path, pcm: bytes) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def dialog(name: str, turns, speaker_names: dict[str, str]) -> dict:
    silence = b"\x00\x00" * int(GAP * SR)
    pcm = silence
    ref = []
    with tempfile.TemporaryDirectory() as td:
        for i, (voice, lang, text) in enumerate(turns):
            p = Path(td) / f"{i}.wav"
            tts(voice, text, p)
            chunk = read_pcm(p)
            start = len(pcm) / 2 / SR
            pcm += chunk
            ref.append({"speaker": speaker_names[voice], "lang": lang, "text": text,
                        "start": round(start, 3), "end": round(len(pcm) / 2 / SR, 3)})
            pcm += silence
    write_pcm(OUT / f"{name}.wav", pcm)
    return {"file": f"{name}.wav", "turns": ref, "duration": round(len(pcm) / 2 / SR, 3)}


def overlap_fixture() -> dict:
    """Two voices speaking at the same time in the middle."""
    import array

    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "a.wav", Path(td) / "b.wav"
        tts(RU, "Я считаю, что мы должны сначала утвердить смету, а потом переходить к закупкам.", a)
        tts(KK, "Жоқ, менің ойымша алдымен жеткізушілермен келісу керек.", b)
        sa, sb = array.array("h", read_pcm(a)), array.array("h", read_pcm(b))
    offset = int(len(sa) * 0.5)
    total = max(len(sa), offset + len(sb))
    mix = array.array("h", [0] * total)
    for i, v in enumerate(sa):
        mix[i] = v // 2
    for i, v in enumerate(sb):
        mix[offset + i] = max(-32768, min(32767, mix[offset + i] + v // 2))
    write_pcm(OUT / "overlap.wav", mix.tobytes())
    return {"file": "overlap.wav", "overlap_start": round(offset / SR, 3),
            "overlap_end": round(min(len(sa), offset + len(sb)) / SR, 3)}


def main() -> int:
    if not shutil.which("say") or not shutil.which("ffmpeg"):
        print("needs macOS `say` and ffmpeg")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"note": "SIMULATED TTS voices (macOS Milena ru_RU, Aru kk_KZ); not real speakers",
                      "speakers": {"Milena": "anna", "Aru": "dana", "Milena+pitch": "guest"},
                      "enroll": {}, "recordings": {}}
    for name, (voice, text) in ENROLL.items():
        tts(voice, text, OUT / f"{name}.wav")
        manifest["enroll"][name] = {"file": f"{name}.wav", "speaker": manifest["speakers"][voice],
                                    "duration": round(len(read_pcm(OUT / f"{name}.wav")) / 2 / SR, 3)}
    names = manifest["speakers"]
    manifest["recordings"]["meeting_ru_kk"] = dialog("meeting_ru_kk", MEETING, names)
    manifest["recordings"]["kk_only"] = dialog("kk_only", KK_ONLY, names)
    manifest["recordings"]["mixed"] = dialog("mixed", MIXED, names)
    manifest["recordings"]["unknown_voice"] = dialog("unknown_voice", UNKNOWN_VOICE, names)
    manifest["recordings"]["overlap"] = overlap_fixture()
    write_pcm(OUT / "silence.wav", b"\x00\x00" * SR * 5)
    manifest["recordings"]["silence"] = {"file": "silence.wav", "duration": 5.0}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    for p in sorted(OUT.glob("*.wav")):
        print(f"{p.name}: {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
