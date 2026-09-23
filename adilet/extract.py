from __future__ import annotations

import argparse
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from deadline_utils import parse_deadline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("transcript_path")
    parser.add_argument("meeting_date", nargs="?", default="2026-09-23")
    parser.add_argument("--ollama-url", default="http://localhost:11434/api/generate")
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--chunk-chars", type=int, default=7000)
    parser.add_argument("--chunk-overlap-segments", type=int, default=3)
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def load_transcript(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, list):
        segments = payload
    else:
        segments = payload.get("segments", [])

    normalized: List[Dict[str, Any]] = []
    for index, segment in enumerate(segments):
        normalized.append(
            {
                "id": int(segment.get("id", index)),
                "start": float(segment["start"]),
                "end": float(segment.get("end", segment["start"])),
                "text": (segment.get("text") or "").strip(),
                "speaker": segment.get("speaker"),
            }
        )
    return normalized


def render_segment(segment: Dict[str, Any]) -> str:
    speaker = segment["speaker"] or "UNKNOWN"
    return (
        f"SEGMENT {segment['id']} | {segment['start']:.1f}-{segment['end']:.1f}"
        f" | speaker={speaker} | {segment['text']}"
    )


def build_chunks(
    segments: Sequence[Dict[str, Any]],
    chunk_chars: int,
    overlap_segments: int,
) -> List[List[Dict[str, Any]]]:
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_chars = 0

    for segment in segments:
        segment_text = render_segment(segment)
        if current and current_chars + len(segment_text) + 1 > chunk_chars:
            chunks.append(current)
            current = current[-overlap_segments:] if overlap_segments else []
            current_chars = sum(len(render_segment(item)) + 1 for item in current)
        current.append(segment)
        current_chars += len(segment_text) + 1

    if current:
        chunks.append(current)
    return chunks


def ask_ollama(
    prompt: str,
    ollama_url: str,
    model: str,
    num_ctx: int,
    seed: int,
    request_timeout: int,
    json_mode: bool = True,
) -> str:
    import requests

    response = requests.post(
        ollama_url,
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            **({"format": "json"} if json_mode else {}),
            "options": {
                "temperature": 0,
                "top_p": 0.1,
                "seed": seed,
                "num_ctx": num_ctx,
                **({} if json_mode else {"num_predict": 512}),
            },
        },
        timeout=request_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    return payload["response"]


def extract_json(text: str) -> Any:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?", "", clean).strip()
        clean = re.sub(r"```$", "", clean).strip()

    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(clean[start : end + 1])
        start = clean.find("[")
        end = clean.rfind("]")
        if start != -1 and end != -1 and end > start:
            return json.loads(clean[start : end + 1])
        raise


def normalize_assignee(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    clean = re.sub(r"\s+", " ", value).strip()
    return clean or None


def normalize_task(raw_task: Dict[str, Any], known_segment_ids: set[int]) -> Dict[str, Any]:
    description = re.sub(r"\s+", " ", str(raw_task.get("description") or "")).strip()
    deadline_source = raw_task.get("deadline_source")
    if deadline_source is not None:
        deadline_source = re.sub(r"\s+", " ", str(deadline_source)).strip() or None

    raw_segment_ids = raw_task.get("segment_ids") or raw_task.get("source_segment_ids") or []
    segment_ids: List[int] = []
    for item in raw_segment_ids:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value in known_segment_ids:
            segment_ids.append(value)

    return {
        "description": description,
        "assignee": normalize_assignee(raw_task.get("assignee")),
        "deadline_source": deadline_source,
        "segment_ids": sorted(set(segment_ids)),
        "needs_review": raw_task.get("needs_review"),
    }


def build_source_text(task: Dict[str, Any], segment_by_id: Dict[int, Dict[str, Any]]) -> str:
    parts = []
    for segment_id in task["segment_ids"]:
        segment = segment_by_id.get(segment_id)
        if segment and segment["text"]:
            parts.append(segment["text"])
    return " ".join(parts).strip()


def task_from_segments(task: Dict[str, Any], segment_by_id: Dict[int, Dict[str, Any]], meeting_date: str) -> Dict[str, Any]:
    source_text = build_source_text(task, segment_by_id)
    starts = [segment_by_id[segment_id]["start"] for segment_id in task["segment_ids"] if segment_id in segment_by_id]
    speakers = [segment_by_id[segment_id]["speaker"] for segment_id in task["segment_ids"] if segment_id in segment_by_id]
    deadline, deadline_needs_review = parse_deadline(task["deadline_source"], meeting_date)

    explicit_review = task.get("needs_review")
    if isinstance(explicit_review, bool):
        needs_review = explicit_review or deadline_needs_review or not task["assignee"] or not source_text
    else:
        needs_review = deadline_needs_review or not task["assignee"] or not source_text

    return {
        "description": task["description"],
        "assignee": task["assignee"],
        "deadline": deadline,
        "deadline_source": task["deadline_source"],
        "source_text": source_text,
        "source_start": min(starts) if starts else 0.0,
        "source_segment_ids": task["segment_ids"],
        "source_speakers": sorted({speaker for speaker in speakers if speaker}),
        "needs_review": needs_review,
    }



def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def same_task(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if left["source_segment_ids"] and right["source_segment_ids"]:
        if set(left["source_segment_ids"]) == set(right["source_segment_ids"]):
            return True

    if abs(left["source_start"] - right["source_start"]) <= 2:
        if similarity(left["description"], right["description"]) >= 0.9:
            return True
        if similarity(left["source_text"], right["source_text"]) >= 0.9:
            return True

    if left["assignee"] == right["assignee"] and left["deadline_source"] == right["deadline_source"]:
        if similarity(left["description"], right["description"]) >= 0.93:
            return True

    return False


def merge_tasks(tasks: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: (item["source_start"], item["description"])):
        if not task["description"]:
            continue
        duplicate = next((existing for existing in merged if same_task(existing, task)), None)
        if duplicate is None:
            merged.append(task)
            continue

        if len(task["source_segment_ids"]) > len(duplicate["source_segment_ids"]):
            duplicate["source_segment_ids"] = task["source_segment_ids"]
            duplicate["source_text"] = task["source_text"]
        if not duplicate["assignee"] and task["assignee"]:
            duplicate["assignee"] = task["assignee"]
        if not duplicate["deadline"] and task["deadline"]:
            duplicate["deadline"] = task["deadline"]
        if not duplicate["deadline_source"] and task["deadline_source"]:
            duplicate["deadline_source"] = task["deadline_source"]
        duplicate["needs_review"] = duplicate["needs_review"] or task["needs_review"]
        duplicate["source_speakers"] = sorted(set(duplicate["source_speakers"]) | set(task["source_speakers"]))
    return merged


def extraction_prompt(chunk: Sequence[Dict[str, Any]], meeting_date: str) -> str:
    transcript = "\n".join(render_segment(segment) for segment in chunk)
    return f"""Ты анализируешь транскрипт делового совещания.
Дата совещания: {meeting_date}

Найди ВСЕ поручения, которые явно или неявно прозвучали в тексте этого фрагмента.

Правила:
1. НЕ вычисляй абсолютные даты. Возвращай только deadline_source ровно так, как срок сказан в транскрипте.
2. description пиши кратко и по делу.
3. assignee копируй строго из транскрипта, без исправления имен и без нормализации.
4. Используй только segment_ids из фрагмента. Можно указывать несколько id, если поручение растянуто на несколько реплик.
5. needs_review ставь true, если поручение двусмысленно, срок размыт, исполнитель неочевиден или в данных есть сомнение.
6. Ничего не выдумывай. Если поручений нет, верни пустой массив.

Верни JSON-объект вида:
{{
  "tasks": [
    {{
      "description": "string",
      "assignee": "string or null",
      "deadline_source": "string or null",
      "segment_ids": [1, 2],
      "needs_review": true
    }}
  ]
}}

Транскрипт:
{transcript}
"""


def summary_prompt(segments: Sequence[Dict[str, Any]]) -> str:
    transcript = "\n".join(render_segment(segment) for segment in segments)
    return f"""Кратко опиши совещание по транскрипту.

Требования:
1. 3-5 предложений.
2. Отрази основные темы, ключевые решения и открытые вопросы.
3. Не добавляй заголовки и markdown.
4. Не выдумывай факты.

Транскрипт:
{transcript}
"""


def main() -> None:
    args = parse_args()
    transcript_path = Path(args.transcript_path)
    segments = load_transcript(transcript_path)
    segment_by_id = {segment["id"]: segment for segment in segments}
    known_segment_ids = set(segment_by_id)

    chunks = build_chunks(segments, args.chunk_chars, args.chunk_overlap_segments)
    raw_tasks: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        print(f"=== Извлекаю поручения из чанка {index}/{len(chunks)} ===")
        response_text = ask_ollama(
            prompt=extraction_prompt(chunk, args.meeting_date),
            ollama_url=args.ollama_url,
            model=args.model,
            num_ctx=args.num_ctx,
            seed=args.seed,
            request_timeout=args.request_timeout,
        )
        payload = extract_json(response_text)
        items = payload if isinstance(payload, list) else payload.get("tasks", [])
        for raw_task in items:
            if isinstance(raw_task, dict):
                raw_tasks.append(normalize_task(raw_task, known_segment_ids))

    tasks = merge_tasks(
        task_from_segments(task, segment_by_id, args.meeting_date)
        for task in raw_tasks
        if task["segment_ids"]
    )

    print(f"Найдено поручений: {len(tasks)}")
    print("=== Формирую саммари... ===")
    summary = ask_ollama(
        prompt=summary_prompt(segments),
        ollama_url=args.ollama_url,
        model=args.model,
        num_ctx=args.num_ctx,
        seed=args.seed,
        request_timeout=args.request_timeout,
        json_mode=False,
    ).strip()
    print(summary)

    output_path = transcript_path.with_name(f"{transcript_path.stem}_result.json")
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "summary": summary,
                "tasks": tasks,
                "metadata": {
                    "meeting_date": args.meeting_date,
                    "model": args.model,
                    "chunk_chars": args.chunk_chars,
                    "chunk_overlap_segments": args.chunk_overlap_segments,
                    "num_ctx": args.num_ctx,
                },
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    print(f"=== Сохранено в {output_path} ===")


if __name__ == "__main__":
    main()