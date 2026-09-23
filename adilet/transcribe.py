from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from faster_whisper import WhisperModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_path")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--language", default=None, help="Force a language code such as ru or kk.")
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument(
        "--diarization-model",
        default="pyannote/speaker-diarization-3.1",
        help="Hugging Face model id or local path for pyannote diarization.",
    )
    parser.add_argument("--hf-token", default=None, help="HF token if the diarization model is not cached locally.")
    return parser.parse_args()


def load_diarization_pipeline(model_name: str, hf_token: Optional[str]):
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained(model_name, use_auth_token=hf_token)


def diarize_audio(audio_path: str, model_name: str, hf_token: Optional[str]) -> List[Dict[str, Any]]:
    pipeline = load_diarization_pipeline(model_name, hf_token)
    diarization = pipeline(audio_path)
    speaker_turns: List[Dict[str, Any]] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        speaker_turns.append(
            {
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": speaker,
            }
        )
    return speaker_turns


def overlap_seconds(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign_speaker(segment: Dict[str, Any], speaker_turns: List[Dict[str, Any]]) -> Optional[str]:
    best_speaker = None
    best_overlap = 0.0
    for turn in speaker_turns:
        current_overlap = overlap_seconds(segment["start"], segment["end"], turn["start"], turn["end"])
        if current_overlap > best_overlap:
            best_overlap = current_overlap
            best_speaker = turn["speaker"]
    return best_speaker


def segment_to_dict(index: int, segment: Any, speaker: Optional[str]) -> Dict[str, Any]:
    item = {
        "id": index,
        "start": float(segment.start),
        "end": float(segment.end),
        "text": (segment.text or "").strip(),
        "speaker": speaker,
    }
    if getattr(segment, "words", None):
        item["words"] = [
            {
                "start": float(word.start) if word.start is not None else None,
                "end": float(word.end) if word.end is not None else None,
                "word": word.word,
                "probability": float(word.probability) if word.probability is not None else None,
            }
            for word in segment.words
        ]
    return item


def main() -> None:
    args = parse_args()
    audio_path = Path(args.audio_path)

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    segments_iter, info = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language=args.language,
        vad_filter=True,
    )
    whisper_segments = list(segments_iter)

    speaker_turns: List[Dict[str, Any]] = []
    if args.diarize:
        speaker_turns = diarize_audio(str(audio_path), args.diarization_model, args.hf_token)

    segments: List[Dict[str, Any]] = []
    for index, segment in enumerate(whisper_segments):
        raw_segment = {
            "start": float(segment.start),
            "end": float(segment.end),
        }
        speaker = assign_speaker(raw_segment, speaker_turns) if speaker_turns else None
        item = segment_to_dict(index=index, segment=segment, speaker=speaker)
        print(
            f"[{item['start']:.2f}s -> {item['end']:.2f}s]"
            f"{f' [{speaker}]' if speaker else ''} {item['text']}"
        )
        segments.append(item)

    payload = {
        "metadata": {
            "source_audio": str(audio_path),
            "stt_model": args.model,
            "language": info.language,
            "language_probability": float(info.language_probability),
            "language_forced": args.language is not None,
            "diarization_enabled": args.diarize,
            "diarization_model": args.diarization_model if args.diarize else None,
        },
        "speaker_turns": speaker_turns,
        "segments": segments,
    }

    output_path = audio_path.with_name(f"{audio_path.stem}_transcript.json")
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    print(f"Detected language: {info.language} (prob {info.language_probability:.2f})")
    print(f"Saved to {output_path}")


if __name__ == "__main__":
    main()