"""Read the target group's timetable from the college's grid PDF."""

from __future__ import annotations

import io
import bisect
import os
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import pdfplumber
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps


PERIODS = [
    ("08:00", "09:40"),
    ("09:50", "11:30"),
    ("11:50", "13:30"),
    ("13:40", "15:20"),
    ("15:40", "17:20"),
    ("17:30", "19:10"),
    ("19:20", "21:00"),
]
GROUP = os.getenv("SCHEDULE_GROUP", "").strip()


@dataclass
class Lesson:
    period: int
    last_period: int
    start: str
    end: str
    subject: str
    teacher: str
    room: str
    raw: str
    half: int | None = None
    explicit_time: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def schedule_date(filename: str) -> date:
    """The sender's file name is authoritative; the printed date can be stale."""
    match = re.search(r"(?<!\d)(\d{2})[._-](\d{2})[._-](20\d{2})(?!\d)", Path(filename).name)
    if not match:
        raise ValueError("PDF filename has no DD.MM.YYYY date")
    day, month, year = map(int, match.groups())
    return date(year, month, day)


def _norm_group(value: str) -> str:
    value = value.upper().replace("K", "К").replace("Қ", "К")
    return re.sub(r"[^0-9К]", "", value)


def _ocr(image: Image.Image, psm: int = 6) -> str:
    image = ImageOps.autocontrast(image.convert("L"))
    return pytesseract.image_to_string(image, lang="rus+eng", config=f"--psm {psm}").strip()


def _render_page(data: bytes, page_number: int, dpi: int = 240) -> Image.Image:
    return convert_from_bytes(data, dpi=dpi, first_page=page_number, last_page=page_number)[0]


def _crop(image: Image.Image, page, x0: float, top: float, x1: float, bottom: float, pad: float = 2) -> Image.Image:
    sx, sy = image.width / page.width, image.height / page.height
    return image.crop((
        max(0, round((x0 + pad) * sx)),
        max(0, round((top + pad) * sy)),
        min(image.width, round((x1 - pad) * sx)),
        min(image.height, round((bottom - pad) * sy)),
    ))


def _row_edges(page) -> list[float]:
    # The PDF draws the table as thin filled rectangles, not line objects.
    vals = [r["top"] for r in page.rects if r["width"] > 50 and r["height"] < 1 and r["x0"] < 40]
    return sorted({round(v, 1) for v in vals})


def _column_edges(page) -> list[float]:
    # Header has the eight stable columns even when a lesson merges cells below.
    hs = _row_edges(page)
    if len(hs) < 2:
        raise ValueError("No timetable grid")
    segments = sorted((r for r in page.rects if r["width"] > 50 and r["height"] < 1 and abs(r["top"] - hs[0]) < 0.5), key=lambda r: r["x0"])
    edges = [segments[0]["x0"]] + [r["x1"] for r in segments] if segments else []
    if len(edges) < 9:
        raise ValueError("Could not locate seven lesson columns")
    return edges[:9]


def _find_row(page, image: Image.Image, group: str) -> tuple[float, float] | None:
    edges = _row_edges(page)
    if len(edges) < 4:
        return None
    x0, x1 = _column_edges(page)[:2]
    for top, bottom in zip(edges[1:], edges[2:]):
        if bottom - top < 16:
            continue
        label = _norm_group(_ocr(_crop(image, page, x0, top, x1, bottom), psm=7))
        if label == _norm_group(group):
            return top, bottom
    return None


def _cell_boundaries(page, top: float, bottom: float, col_edges: list[float]) -> list[float]:
    middle = (top + bottom) / 2
    present = {round(col_edges[1], 1), round(col_edges[-1], 1)}
    for r in page.rects:
        if (r["width"] < 1 and r["height"] > 10 and r["top"] < middle < r["bottom"]
                and col_edges[1] < r["x0"] < col_edges[-1]):
            present.add(round(r["x0"], 1))
    return sorted(present)


def _period_at(x: float, col_edges: list[float]) -> int:
    return max(1, min(7, bisect.bisect_right(col_edges[1:], x)))


def _cell_vertical_span(page, a: float, b: float, top: float, bottom: float) -> tuple[float, float]:
    """A cell may be shared vertically by several group rows."""
    horizontal = [r for r in page.rects if r["width"] > 5 and r["height"] < 1
                  and r["x1"] > a and r["x0"] < b]
    candidates = sorted({round(r["top"], 1) for r in horizontal})
    lines = []
    for y in candidates:
        segments = sorted((max(a, r["x0"]), min(b, r["x1"])) for r in horizontal if abs(r["top"] - y) < 0.6)
        covered = 0.0
        last_end = a
        for start, end in segments:
            if end > last_end:
                covered += end - max(start, last_end)
                last_end = end
        if covered >= 0.7 * (b - a):
            lines.append(y)
    starts = [value for value in lines if value <= top + 1]
    ends = [value for value in lines if value >= bottom - 1]
    return (max(starts) if starts else top, min(ends) if ends else bottom)


def _clean_lines(raw: str) -> list[str]:
    return [re.sub(r"\s+", " ", x).strip(" .-—") for x in raw.splitlines() if x.strip()]


TIME_RANGE = re.compile(r"(?<!\d)(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})(?!\d)")


def _parse_lesson(raw: str, first: int, last: int, half: int | None = None, time_hint: str = "") -> Lesson | None:
    lines = _clean_lines(raw)
    if not lines:
        return None
    time_match = TIME_RANGE.search(time_hint) or TIME_RANGE.search(raw)
    start, end = PERIODS[first - 1][0], PERIODS[last - 1][1]
    if half is not None:
        from datetime import datetime, timedelta
        period_start = datetime.strptime(start, "%H:%M")
        if half == 1:
            end = (period_start + timedelta(minutes=45)).strftime("%H:%M")
        else:
            start = (period_start + timedelta(minutes=55)).strftime("%H:%M")
    if time_match:
        a, b, c, d = map(int, time_match.groups())
        start, end = f"{a:02d}:{b:02d}", f"{c:02d}:{d:02d}"
        lines = [x for x in lines if not TIME_RANGE.search(x)]
    # A room is normally the bottom line: 313, 418 415, 2к11 216, or с/з.
    room = ""
    if lines and re.search(r"\d|\bс/з\b|\bа/з\b|\bауд\b", lines[-1], re.I):
        room = lines.pop()
    room = re.sub(r"(?<=\d)[xхk](?=\d)", "к", room, flags=re.I)
    if lines and re.match(r"^Физ\s*к\s*и\s*зд\b", lines[0], re.I):
        match = re.match(r"^(Физ\s*к\s*и\s*зд)\s*(.*)$", lines[0], re.I)
        teacher = " / ".join(([match.group(2)] if match.group(2) else []) + lines[1:])
        subject = "Физ к и зд"
    else:
        teacher = lines.pop() if len(lines) > 1 else ""
        subject = " ".join(lines).strip() or "Занятие"
    if re.sub(r"\W", "", subject.casefold()) == "информационныйчас":
        subject = "Информационный час"
    return Lesson(first, last, start, end, subject, teacher, room, raw.strip(), half, bool(time_match))


def _parse_row(page, image: Image.Image, top: float, bottom: float, columns: list[float]) -> list[Lesson]:
    boundaries = _cell_boundaries(page, top, bottom, columns)
    lessons = []
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a < 5:
            continue
        first, last = _period_at(a + 0.5, columns), _period_at(b - 0.5, columns)
        midpoint = (columns[first] + columns[first + 1]) / 2
        width = columns[first + 1] - columns[first]
        half = None
        if first == last and b - a < width * 0.7:
            half = 1 if (a + b) / 2 < midpoint else 2
        cell_top, cell_bottom = _cell_vertical_span(page, a, b, top, bottom)
        cell = _crop(image, page, a, cell_top, b, cell_bottom)
        raw = _ocr(cell)
        text_layer = page.crop((a, cell_top, b, cell_bottom)).extract_text() or ""
        lesson = _parse_lesson(raw, first, last, half, text_layer)
        if lesson:
            lessons.append(lesson)
    return _merge_practice(lessons)


def _merge_practice(lessons: list[Lesson]) -> list[Lesson]:
    merged = []
    for lesson in lessons:
        if (merged and "практик" in lesson.subject.casefold() and not lesson.explicit_time and lesson.half is None
                and "практик" in merged[-1].subject.casefold() and not merged[-1].explicit_time
                and merged[-1].half is None and merged[-1].last_period + 1 == lesson.period
                and (merged[-1].subject, merged[-1].teacher, merged[-1].room) == (lesson.subject, lesson.teacher, lesson.room)):
            merged[-1].last_period = lesson.last_period
            merged[-1].end = lesson.end
            merged[-1].raw += "\n" + lesson.raw
        else:
            merged.append(lesson)
    return merged


def parse_pdf(data: bytes, filename: str, group: str = GROUP) -> dict:
    if not group:
        raise ValueError("SCHEDULE_GROUP must be configured")
    day = schedule_date(filename)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            if len(_row_edges(page)) < 4:
                continue
            image = _render_page(data, index)
            row = _find_row(page, image, group)
            if not row:
                continue
            top, bottom = row
            columns = _column_edges(page)
            lessons = _parse_row(page, image, top, bottom, columns)
            return {"date": day.isoformat(), "group": group, "page": index, "lessons": [x.to_dict() for x in lessons]}
    raise ValueError(f"Group {group} not found in PDF")
