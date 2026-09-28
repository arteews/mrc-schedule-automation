"""Every change to this group's lessons announces an updated timetable."""

import copy
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import worker


tomorrow = (datetime.now(worker.TZ).date() + timedelta(days=1)).isoformat()
base = {
    "date": tomorrow,
    "group": "1К0000",
    "page": 2,
    "lessons": [
        {
            "period": 4,
            "last_period": 4,
            "start": "13:40",
            "end": "15:20",
            "subject": "ОП",
            "teacher": "Преподаватель А",
            "room": "313",
            "raw": "ОП\nПреподаватель А\n313",
        }
    ],
}

metadata_update = copy.deepcopy(base)
metadata_update["page"] = 3
metadata_update["lessons"][0]["raw"] = "OCR spacing changed"
assert worker.notification_signature(base) == worker.notification_signature(metadata_update)

room_update = copy.deepcopy(metadata_update)
room_update["lessons"][0]["room"] = "315"
room_update["lessons"][0]["raw"] = "ОП\nПреподаватель А\n315"
assert worker.notification_signature(base) != worker.notification_signature(room_update)
assert worker.describe_changes(base, room_update) == ["4-я пара: кабинет: 313 → 315"]

teacher_update = copy.deepcopy(room_update)
teacher_update["lessons"][0]["teacher"] = "Другой преподаватель"
assert worker.notification_signature(base) != worker.notification_signature(teacher_update)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    inbox = root / "inbox"
    inbox.mkdir()
    with patch.object(worker, "DB", root / "schedule.sqlite3"), patch.object(worker, "INBOX", inbox), patch.object(worker, "CHAT_ID", "test-chat"):
        worker.init_db()
        for message_id, schedule in enumerate((base, metadata_update, room_update, teacher_update, base), start=1):
            path = inbox / f"{message_id}.pdf"
            path.write_bytes(b"fake PDF; parser mocked")
            with worker.connect() as db:
                db.execute(
                    "INSERT INTO documents(message_id,filename,path) VALUES(?,?,?)",
                    (message_id, "schedule.pdf", str(path)),
                )
            with patch.object(worker, "parse_pdf", return_value=schedule):
                worker.process_one_pdf()
            with worker.connect() as db:
                stored = json.loads(db.execute("SELECT payload FROM schedules WHERE day=?", (tomorrow,)).fetchone()[0])
                messages = db.execute("SELECT text FROM outbox ORDER BY id").fetchall()
            assert stored["lessons"][0]["room"] == schedule["lessons"][0]["room"]
            assert len(messages) == (1 if message_id < 3 else message_id - 1)
            if message_id == 3:
                assert "кабинет: 313 → 315" in messages[-1][0]
                assert "каб. 315" in messages[-1][0]

print("change notification checks: ok")
