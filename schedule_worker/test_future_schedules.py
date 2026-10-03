"""A Monday PDF received on Saturday must notify and sync without waiting."""
import copy
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import worker


class Clock(datetime):
    instant = datetime(2030, 10, 12, 12, 0, tzinfo=worker.TZ)  # Saturday

    @classmethod
    def now(cls, tz=None):
        return cls.instant.astimezone(tz) if tz else cls.instant.replace(tzinfo=None)


monday = {
    "date": "2030-10-14", "group": "1К0000", "lessons": [{
        "period": 4, "last_period": 4, "half": None,
        "start": "13:40", "end": "15:20", "subject": "Предмет",
        "teacher": "Преподаватель А", "room": "101",
    }],
}
with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "datetime", Clock), \
        patch.object(worker, "DB", Path(tmp) / "schedule.sqlite3"), \
        patch.object(worker, "CHAT_ID", "test-chat"):
    worker.init_db()

    def receive(message_id, schedule):
        path = Path(tmp) / f"{message_id}.pdf"
        path.write_bytes(b"PDF parsing is mocked")
        with worker.connect() as db:
            db.execute("INSERT INTO documents(message_id,filename,path) VALUES(?,?,?)",
                       (message_id, "schedule.pdf", str(path)))
        with patch.object(worker, "parse_pdf", return_value=schedule):
            worker.process_one_pdf()

    def messages():
        with worker.connect() as db:
            return db.execute("SELECT text FROM outbox ORDER BY id").fetchall()

    def job(day):
        with worker.connect() as db:
            return db.execute("SELECT * FROM calendar_jobs WHERE day=?", (day,)).fetchone()

    receive(1, monday)
    assert len(messages()) == 1
    assert "В понедельник (14.10.2030) к 4-й паре" in messages()[0]["text"]
    assert "Завтра" not in messages()[0]["text"]
    assert job(monday["date"])["revision"] == 1
    assert json.loads(job(monday["date"])["payload"])["start"] == "2030-10-14T13:40:00+03:00"
    saturday_payload = worker.calendar_payload(monday)

    receive(2, copy.deepcopy(monday))
    assert len(messages()) == 1  # Other groups changing in the PDF is irrelevant.
    changed = copy.deepcopy(monday)
    changed["lessons"][0]["room"] = "102"
    receive(3, changed)
    assert len(messages()) == 2 and "кабинет: 101 → 102" in messages()[-1]["text"]
    assert job(monday["date"])["revision"] == 2

    Clock.instant += timedelta(days=1)  # Sunday: same file must not duplicate.
    receive(4, changed)
    assert len(messages()) == 2 and job(monday["date"])["revision"] == 2
    assert "Завтра к 4-й паре" in worker.format_tomorrow_html(monday)
    assert worker.calendar_payload(monday) == saturday_payload  # Stable description.
    receive(5, monday)  # Returning to an earlier version is a real change.
    assert len(messages()) == 3 and job(monday["date"])["revision"] == 3

    cancelled = copy.deepcopy(monday)
    cancelled["lessons"] = []
    receive(6, cancelled)
    assert len(messages()) == 4 and "Завтра занятий нет" in messages()[-1]["text"]
    assert json.loads(job(monday["date"])["payload"])["action"] == "delete"

    past = copy.deepcopy(monday)
    past["date"] = "2030-10-11"
    receive(7, past)
    assert len(messages()) == 4 and job(past["date"]) is None

    # Already parsed files affected by the old tomorrow filter are recovered.
    future = copy.deepcopy(monday)
    future["date"] = "2030-10-16"
    with worker.connect() as db:
        db.execute("INSERT INTO schedules(day,content_hash,payload) VALUES(?,?,?)",
                   (future["date"], "test", json.dumps(future)))
    worker.seed_future_calendar()
    assert job(future["date"])["revision"] == 1
    assert job(past["date"]) is None
    worker.seed_future_calendar()
    assert job(future["date"])["revision"] == 1
    assert job(monday["date"])["revision"] == 4

    Clock.instant += timedelta(days=1)
    assert "Сегодня к 4-й паре" in worker.format_tomorrow_html(monday)
    assert worker.calendar_payload(monday) == saturday_payload
    today_update = copy.deepcopy(monday)
    today_update["lessons"][0]["start"] = "14:35"
    receive(8, today_update)
    assert "Сегодня к 4-й паре" in messages()[-1]["text"]
    assert json.loads(job(monday["date"])["payload"])["start"] == "2030-10-14T14:35:00+03:00"

print("future schedule notification and calendar checks: ok")
