from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional, Tuple


MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

WEEKDAY_FORMS_RU = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнику": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнику": 1,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "среде": 2,
    "четверг": 3,
    "четверга": 3,
    "четвергу": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "пятнице": 4,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "субботе": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "воскресенью": 6,
}


def _normalize_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text.lower()).strip()
    return compact.strip(".,;:!?")


def _next_weekday(base_date: date, weekday: int) -> date:
    delta_days = (weekday - base_date.weekday()) % 7
    if delta_days == 0:
        delta_days = 7
    return base_date + timedelta(days=delta_days)


def _parse_day_month(text: str, meeting_date: date) -> Optional[date]:
    match = re.search(
        r"\b(?:до|к|на)?\s*(\d{1,2})\s+("
        + "|".join(MONTHS_RU.keys())
        + r")(?:\s+(\d{4}))?\b",
        text,
    )
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS_RU[match.group(2)]
    year = int(match.group(3)) if match.group(3) else meeting_date.year

    try:
        candidate = date(year, month, day)
    except ValueError:
        return None

    if not match.group(3) and candidate < meeting_date:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate


def _parse_numeric_date(text: str, meeting_date: date) -> Optional[date]:
    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if iso_match:
        return date(
            int(iso_match.group(1)),
            int(iso_match.group(2)),
            int(iso_match.group(3)),
        )

    local_match = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b", text)
    if not local_match:
        return None

    day = int(local_match.group(1))
    month = int(local_match.group(2))
    year = int(local_match.group(3)) if local_match.group(3) else meeting_date.year
    try:
        candidate = date(year, month, day)
    except ValueError:
        return None

    if not local_match.group(3) and candidate < meeting_date:
        try:
            candidate = date(year + 1, month, day)
        except ValueError:
            return None
    return candidate


def parse_deadline(deadline_source: Optional[str], meeting_date_iso: str) -> Tuple[Optional[str], bool]:
    if not deadline_source:
        return None, True

    meeting_date = date.fromisoformat(meeting_date_iso)
    text = _normalize_text(deadline_source)

    if any(phrase in text for phrase in ("на следующей неделе", "на этой неделе")):
        return None, True

    if "сегодня" in text:
        return meeting_date.isoformat(), False
    if "послезавтра" in text:
        return (meeting_date + timedelta(days=2)).isoformat(), False
    if "завтра" in text:
        return (meeting_date + timedelta(days=1)).isoformat(), False

    numeric_date = _parse_numeric_date(text, meeting_date)
    if numeric_date:
        return numeric_date.isoformat(), False

    textual_date = _parse_day_month(text, meeting_date)
    if textual_date:
        return textual_date.isoformat(), False

    weekday_match = re.search(
        r"\b(?:до|к|на|во|в)?\s*("
        + "|".join(sorted(WEEKDAY_FORMS_RU.keys(), key=len, reverse=True))
        + r")\b",
        text,
    )
    if weekday_match:
        candidate = _next_weekday(meeting_date, WEEKDAY_FORMS_RU[weekday_match.group(1)])
        return candidate.isoformat(), False

    return None, True