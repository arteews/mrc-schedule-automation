"""Bell times follow actual lessons, including half lessons and practice."""

from datetime import datetime, timedelta

import worker


def lesson(period, start, end, *, last_period=None, half=None, subject="Предмет"):
    return {
        "period": period,
        "last_period": last_period or period,
        "half": half,
        "start": start,
        "end": end,
        "subject": subject,
    }


school_day = [
    lesson(4, "13:40", "15:20"),
    lesson(5, "15:40", "17:20"),
    lesson(6, "17:30", "19:10"),
]
assert worker.lesson_breaks(school_day) == [
    ("14:25", "14:35"),
    ("15:20", "15:40"),
    ("16:25", "16:35"),
    ("17:20", "17:30"),
    ("18:15", "18:25"),
]

now = datetime(2030, 10, 15, 14, 25, tzinfo=worker.TZ)
start = worker.bell_candidates(school_day, now)
assert len(start) == 1 and "Начался перерыв: 14:25–14:35" in start[0][1]
assert worker.bell_candidates(school_day, now + timedelta(seconds=91)) == []
end = worker.bell_candidates(school_day, now + timedelta(minutes=10))
assert len(end) == 1 and "Перерыв закончился" in end[0][1]
assert start[0][0] != end[0][0]

halves = [
    lesson(4, "13:40", "14:25", half=1),
    lesson(4, "14:35", "15:20", half=2),
]
assert worker.lesson_breaks(halves) == [("14:25", "14:35")]

practice = [
    lesson(2, "09:50", "15:20", last_period=4, subject="Практика"),
    lesson(5, "15:40", "17:20"),
]
assert worker.lesson_breaks(practice) == [("15:20", "15:40"), ("16:25", "16:35")]

overlapping_subgroups = [
    lesson(4, "13:40", "15:20"),
    lesson(4, "13:40", "15:20", subject="Другая подгруппа"),
]
assert worker.lesson_breaks(overlapping_subgroups) == [("14:25", "14:35")]
assert worker.lesson_breaks([]) == []

print("bell schedule checks: ok")
