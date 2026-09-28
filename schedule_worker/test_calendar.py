"""One calendar event spans the school day and updates in place."""

import copy
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import worker


day = (datetime.now(worker.TZ).date() + timedelta(days=1)).isoformat()
schedule = {
    "date": day,
    "group": "1К0000",
    "lessons": [
        {"period": 1, "last_period": 1, "half": 2, "explicit_time": False,
         "start": "08:55", "end": "09:40", "subject": "Математика", "teacher": "А", "room": "101"},
        {"period": 4, "last_period": 4, "half": 1, "explicit_time": True,
         "start": "13:40", "end": "14:00", "subject": "Информационный час", "teacher": "Б", "room": "202"},
        {"period": 6, "last_period": 6, "half": None, "explicit_time": True,
         "start": "17:30", "end": "19:10", "subject": "Практика", "teacher": "В", "room": "303"},
    ],
}
payload = worker.calendar_payload(schedule)
assert payload["event_id"].startswith("mrc") and payload["event_id"].endswith(f"d{day.replace('-', '')}")
assert payload["start"] == f"{day}T08:55:00+03:00"
assert payload["end"] == f"{day}T19:10:00+03:00"
assert "Информационный час" in payload["description"]
assert "Практика" in payload["description"]

with tempfile.TemporaryDirectory() as tmp, patch.object(worker, "DB", Path(tmp) / "test.sqlite3"):
    worker.init_db()
    worker.queue_calendar(schedule)
    worker.queue_calendar(schedule)
    with worker.connect() as db:
        row = db.execute("SELECT * FROM calendar_jobs WHERE day=?", (day,)).fetchone()
        assert row["revision"] == 1
        assert row["synced_revision"] == 0
        assert json.loads(row["payload"])["start"] == payload["start"]

    changed = copy.deepcopy(schedule)
    changed["lessons"][0]["room"] = "105"
    worker.queue_calendar(changed)
    with worker.connect() as db:
        row = db.execute("SELECT * FROM calendar_jobs WHERE day=?", (day,)).fetchone()
        assert row["revision"] == 2
        assert row["synced_revision"] == 0
        assert json.loads(row["payload"])["event_id"] == payload["event_id"]
        db.execute("UPDATE calendar_jobs SET synced_revision=revision WHERE day=?", (day,))

    cancelled = copy.deepcopy(changed)
    cancelled["lessons"] = []
    worker.queue_calendar(cancelled)
    with worker.connect() as db:
        row = db.execute("SELECT * FROM calendar_jobs WHERE day=?", (day,)).fetchone()
        assert row["revision"] == 3
        assert row["synced_revision"] == 2
        assert json.loads(row["payload"])["action"] == "delete"

print("calendar event checks: ok")
