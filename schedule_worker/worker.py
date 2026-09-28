"""Private schedule intake and durable notification queue for n8n."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

from parser import GROUP, PERIODS, parse_pdf


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
DATA = Path(os.getenv("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "schedule.sqlite3"
INBOX = DATA / "inbox"
INBOX.mkdir(exist_ok=True)
TZ = ZoneInfo("Europe/Minsk")
CHAT_ID = os.getenv("CHAT_ID", "")
SOURCE = os.getenv("SCHEDULE_SOURCE_BOT", "MRC_ScheduleBot").lstrip("@")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "")
N8N_CALENDAR_WEBHOOK_URL = os.getenv("N8N_CALENDAR_WEBHOOK_URL", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WAKE = threading.Event()


def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init_db():
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                message_id INTEGER PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', error TEXT
            );
            CREATE TABLE IF NOT EXISTS schedules (
                day TEXT PRIMARY KEY, content_hash TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, unique_key TEXT NOT NULL UNIQUE,
                chat_id TEXT NOT NULL, text TEXT NOT NULL, sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS calendar_jobs (
                day TEXT PRIMARY KEY, revision INTEGER NOT NULL, synced_revision INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "rich" not in {column["name"] for column in db.execute("PRAGMA table_info(outbox)")}:
            db.execute("ALTER TABLE outbox ADD COLUMN rich INTEGER NOT NULL DEFAULT 0")


def bot_api(method: str, payload: dict) -> dict:
    """Call Telegram Bot API without ever logging the token-bearing URL."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=35) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(f"Bot API {method} failed")
    return result["result"]


def bot_send(text: str):
    bot_api("sendMessage", {"chat_id": CHAT_ID, "text": text})


def bot_commands():
    """Answer optional text commands without adding a reply keyboard."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    with connect() as db:
        row = db.execute("SELECT value FROM bot_meta WHERE key='bot_update_offset'").fetchone()
    offset = int(row["value"]) if row else 0
    while True:
        try:
            updates = bot_api("getUpdates", {"offset": offset, "timeout": 25,
                                             "allowed_updates": ["message"]})
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                with connect() as db:
                    db.execute("INSERT OR REPLACE INTO bot_meta(key,value) VALUES('bot_update_offset',?)", (str(offset),))
                message = update.get("message") or {}
                if str((message.get("chat") or {}).get("id")) != str(CHAT_ID):
                    continue
                command = (message.get("text") or "").strip()
                if command not in ("/calendar", "/start"):
                    continue
                if N8N_CALENDAR_WEBHOOK_URL:
                    bot_send("✅ Google Календарь подключён. Новое расписание на завтра создаёт одно событие на весь учебный день; изменения обновляют его.")
                else:
                    bot_send("Откройте учётные данные Google Календаря в вашей установке n8n, "
                             "нажмите Sign in with Google и подтвердите доступ к мероприятиям календаря.")
        except Exception:
            logging.error("Telegram command polling failed; retrying")
            time.sleep(10)


def enqueue_pdf(message_id: int, filename: str, content: bytes):
    if not filename.lower().endswith(".pdf"):
        return
    path = INBOX / f"{message_id}.pdf"
    with connect() as db:
        if db.execute("SELECT 1 FROM documents WHERE message_id=?", (message_id,)).fetchone():
            return
        path.write_bytes(content)
        db.execute("INSERT INTO documents(message_id,filename,path) VALUES(?,?,?)", (message_id, filename, str(path)))
    logging.info("Queued PDF message %s (%s)", message_id, filename)
    WAKE.set()


def queue_message(key: str, text: str, *, rich: bool = False):
    if not CHAT_ID:
        logging.warning("CHAT_ID missing; notification %s not queued", key)
        return
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO outbox(unique_key,chat_id,text,rich) VALUES(?,?,?,?)",
                   (key, CHAT_ID, text, int(rich)))


def lesson_details(lesson: dict) -> tuple[str, str]:
    room = lesson.get("room", "")
    teacher = lesson.get("teacher", "")
    room_parts = room.split()
    teacher_parts = teacher.split()
    if len(room_parts) > 1 and all(any(char.isdigit() for char in part) for part in room_parts):
        room = " / ".join(room_parts)
        if len(teacher_parts) == len(room_parts) and all("." not in part for part in teacher_parts):
            teacher = " / ".join(teacher_parts)
    return room, teacher


def lesson_halves(lesson: dict) -> list[tuple[int, str, str, str, str]]:
    """Return both 45-minute halves of every complete period in a lesson."""
    if is_practice(lesson) or lesson.get("half"):
        return []
    lesson_start = datetime.strptime(lesson["start"], "%H:%M")
    lesson_end = datetime.strptime(lesson["end"], "%H:%M")
    halves = []
    for period in range(lesson["period"], lesson["last_period"] + 1):
        start_text, end_text = PERIODS[period - 1]
        start = datetime.strptime(start_text, "%H:%M")
        end = datetime.strptime(end_text, "%H:%M")
        if start < lesson_start or end > lesson_end or end - start != timedelta(minutes=100):
            continue
        first_end = start + timedelta(minutes=45)
        second_start = first_end + timedelta(minutes=10)
        halves.append((period, start_text, first_end.strftime("%H:%M"), second_start.strftime("%H:%M"), end_text))
    return halves


def is_practice(lesson: dict) -> bool:
    return "практик" in lesson.get("subject", "").casefold()


def lesson_label(lesson: dict) -> str:
    if is_practice(lesson):
        return lesson["subject"]
    period = lesson["period"]
    if lesson["last_period"] != period:
        return f"{period}–{lesson['last_period']} пары"
    if lesson.get("half"):
        if lesson.get("explicit_time"):
            duration = datetime.strptime(lesson["end"], "%H:%M") - datetime.strptime(lesson["start"], "%H:%M")
            if duration != timedelta(minutes=45):
                return f"{period}-я пара (короткое занятие)"
        return f"{lesson['half']}-я половина {period}-й пары"
    return f"{period}-я пара"


def format_tomorrow(schedule: dict, updated: bool = False, changes: list[str] | None = None) -> str:
    day = date.fromisoformat(schedule["date"])
    heading = "🔄 Обновлено расписание" if updated else "📢 Появилось расписание"
    lines = [f"{heading} на {day:%d.%m.%Y}!", f"Группа {schedule.get('group', GROUP)}"]
    if changes:
        lines += ["", "Что изменилось:"] + [f"• {change}" for change in changes]
        lines.append("")
    lessons = schedule["lessons"]
    if not lessons:
        return "\n".join(lines + ["Завтра занятий нет."])
    first = min(lessons, key=lambda lesson: lesson["start"])
    arrival = (f"Завтра начало в {first['start']}." if is_practice(first)
               else f"Завтра к {first['period']}-й паре, начало в {first['start']}.")
    lines += [arrival, "", "Расписание:"]
    for lesson in lessons:
        room, teacher = lesson_details(lesson)
        label = lesson_label(lesson)
        lines.append(label if is_practice(lesson) else f"{label} — {lesson['subject']}")
        halves = lesson_halves(lesson)
        if halves:
            for period, start, first_end, second_start, end in halves:
                if len(halves) > 1:
                    lines.append(f"  {period}-я пара:")
                lines.append(f"  1-я половина: {start}–{first_end}")
                lines.append(f"  Перерыв: {first_end}–{second_start}")
                lines.append(f"  2-я половина: {second_start}–{end}")
        else:
            lines.append(f"  Время: {lesson['start']}–{lesson['end']}")
        details = []
        if room:
            details.append(f"каб. {room}")
        if teacher:
            details.append(teacher)
        if details:
            lines.append("  " + ", ".join(details))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_tomorrow_html(schedule: dict, updated: bool = False, changes: list[str] | None = None) -> str:
    """Telegram HTML with bold lessons, italic times, and quoted room/teacher."""
    esc = html.escape
    day = date.fromisoformat(schedule["date"])
    heading = "🔄 Обновлено расписание" if updated else "📢 Появилось расписание"
    lines = [f"<b>{heading} на {day:%d.%m.%Y}!</b>",
             f"<b>Группа {esc(str(schedule.get('group', GROUP)))}</b>"]
    if changes:
        lines += ["", "<b>Что изменилось:</b>"] + [f"• {esc(change)}" for change in changes]
        lines.append("")
    lessons = schedule["lessons"]
    if not lessons:
        return "\n".join(lines + ["<b>Завтра занятий нет.</b>"])
    first = min(lessons, key=lambda lesson: lesson["start"])
    arrival = (f"Завтра начало в {first['start']}." if is_practice(first)
               else f"Завтра к {first['period']}-й паре, начало в {first['start']}.")
    lines += [f"<b>{esc(arrival)}</b>", "", "<b>Расписание:</b>"]
    for lesson in lessons:
        room, teacher = lesson_details(lesson)
        label = lesson_label(lesson)
        title = label if is_practice(lesson) else f"{label} — {lesson['subject']}"
        lines.append(f"<b>{esc(title)}</b>")
        halves = lesson_halves(lesson)
        if halves:
            for period, start, first_end, second_start, end in halves:
                if len(halves) > 1:
                    lines.append(f"<b>{period}-я пара:</b>")
                lines.append(f"<i>1-я половина: {esc(start)}–{esc(first_end)}</i>")
                lines.append(f"<i>Перерыв: {esc(first_end)}–{esc(second_start)}</i>")
                lines.append(f"<i>2-я половина: {esc(second_start)}–{esc(end)}</i>")
        else:
            lines.append(f"<i>Время: {esc(lesson['start'])}–{esc(lesson['end'])}</i>")
        details = []
        if teacher:
            details.append(f"👤 {teacher}")
        if room:
            details.append(f"🏫 каб. {room}")
        if details:
            lines.append(f"<blockquote>{esc(' │ '.join(details))}</blockquote>")
        lines.append("")
    return "\n".join(lines).rstrip()


def calendar_payload(schedule: dict) -> dict:
    """One stable Google Calendar event per school day, covering all lessons."""
    day = schedule["date"]
    lessons = schedule["lessons"]
    group = schedule.get("group", GROUP)
    group_key = hashlib.sha256(group.encode()).hexdigest()[:12]
    event_id = f"mrc{group_key}d{day.replace('-', '')}"
    payload = {"day": day, "event_id": event_id, "action": "upsert" if lessons else "delete"}
    if lessons:
        first = min(lessons, key=lambda lesson: lesson["start"])
        last = max(lessons, key=lambda lesson: lesson["end"])
        start = datetime.combine(date.fromisoformat(day), datetime.strptime(first["start"], "%H:%M").time(), TZ)
        end = datetime.combine(date.fromisoformat(day), datetime.strptime(last["end"], "%H:%M").time(), TZ)
        payload.update({
            "summary": f"Учёба {group} — {date.fromisoformat(day):%d.%m.%Y}",
            "description": format_tomorrow(schedule),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "time_zone": "Europe/Minsk",
        })
    return payload


def queue_calendar(schedule: dict):
    payload = json.dumps(calendar_payload(schedule), ensure_ascii=False, sort_keys=True)
    with connect() as db:
        row = db.execute("SELECT revision,payload FROM calendar_jobs WHERE day=?", (schedule["date"],)).fetchone()
        if row and row["payload"] == payload:
            return
        revision = row["revision"] + 1 if row else 1
        db.execute("""INSERT INTO calendar_jobs(day,revision,payload) VALUES(?,?,?)
                      ON CONFLICT(day) DO UPDATE SET revision=excluded.revision,payload=excluded.payload""",
                   (schedule["date"], revision, payload))
    WAKE.set()


def seed_tomorrow_calendar():
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    with connect() as db:
        row = db.execute("SELECT payload FROM schedules WHERE day=?", (tomorrow,)).fetchone()
    if row:
        queue_calendar(json.loads(row["payload"]))


def normalized_value(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def notification_signature(schedule: dict) -> str:
    """Compare only the target group's actual lessons, including rooms."""

    relevant = {
        "date": schedule["date"],
        "group": normalized_value(schedule.get("group", GROUP)),
        "lessons": sorted([
            {
                "period": lesson["period"],
                "last_period": lesson["last_period"],
                "half": lesson.get("half"),
                "start": lesson["start"],
                "end": lesson["end"],
                "subject": normalized_value(lesson.get("subject")),
                "teacher": normalized_value(lesson.get("teacher")),
                "room": normalized_value(lesson.get("room")),
            }
            for lesson in schedule["lessons"]
        ], key=lambda lesson: (lesson["period"], lesson["last_period"], lesson["half"] or 0)),
    }
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def describe_changes(old: dict, new: dict) -> list[str]:
    """Explain the differences before showing the complete updated timetable."""
    def by_period(schedule):
        return {(lesson["period"], lesson["last_period"], lesson.get("half") or 0): lesson for lesson in schedule["lessons"]}

    def label(key):
        return lesson_label((after.get(key) or before[key]))

    before, after = by_period(old), by_period(new)
    changes = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            changes.append(f"{label(key)}: добавлена — {after[key]['subject']}")
            continue
        if key not in after:
            changes.append(f"{label(key)}: отменена — {before[key]['subject']}")
            continue
        previous, current = before[key], after[key]
        parts = []
        for field, name in (("subject", "предмет"), ("teacher", "преподаватель"), ("room", "кабинет")):
            if normalized_value(previous.get(field)) != normalized_value(current.get(field)):
                parts.append(f"{name}: {previous.get(field) or 'не указан'} → {current.get(field) or 'не указан'}")
        previous_time = f"{previous['start']}–{previous['end']}"
        current_time = f"{current['start']}–{current['end']}"
        if previous_time != current_time:
            parts.append(f"время: {previous_time} → {current_time}")
        if parts:
            changes.append(f"{label(key)}: " + "; ".join(parts))
    return changes


def process_one_pdf():
    with connect() as db:
        row = db.execute("SELECT * FROM documents WHERE status='pending' ORDER BY message_id LIMIT 1").fetchone()
    if not row:
        return
    try:
        parsed = parse_pdf(Path(row["path"]).read_bytes(), row["filename"])
        payload = json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        notification_digest = notification_signature(parsed)
        with connect() as db:
            old = db.execute("SELECT payload FROM schedules WHERE day=?", (parsed["date"],)).fetchone()
            previous = json.loads(old["payload"]) if old else None
            changed = not previous or notification_signature(previous) != notification_digest
            db.execute("INSERT INTO schedules(day,content_hash,payload) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET content_hash=excluded.content_hash,payload=excluded.payload", (parsed["date"], digest, payload))
            db.execute("UPDATE documents SET status='done',error=NULL WHERE message_id=?", (row["message_id"],))
        tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        if changed and parsed["date"] == tomorrow:
            changes = describe_changes(previous, parsed) if previous else None
            queue_message(f"tomorrow:{tomorrow}:{row['message_id']}",
                          format_tomorrow_html(parsed, updated=bool(old), changes=changes), rich=True)
            queue_calendar(parsed)
        logging.info("Parsed %s: %s lessons, changed=%s", row["filename"], len(parsed["lessons"]), changed)
    except Exception as exc:
        logging.exception("Could not parse %s", row["filename"])
        with connect() as db:
            db.execute("UPDATE documents SET status='error',error=? WHERE message_id=?", (str(exc)[:500], row["message_id"]))
        queue_message(f"parse-error:{row['message_id']}", f"⚠️ Не удалось прочитать расписание {row['filename']}. Проверьте PDF.")


def reminder_candidates(lessons: list[dict], now: datetime) -> list[tuple[str, str]]:
    day = now.date().isoformat()
    messages = []
    for lesson in lessons:
        end = datetime.combine(now.date(), datetime.strptime(lesson["end"], "%H:%M").time(), TZ)
        target = end - timedelta(minutes=15)
        if not target <= now < target + timedelta(seconds=90):
            continue
        following = min((candidate for candidate in lessons if candidate is not lesson and candidate["start"] >= lesson["end"]),
                        key=lambda candidate: candidate["start"], default=None)
        if following:
            room, teacher = lesson_details(following)
            following_text = (following["subject"] if is_practice(following)
                              else f"{lesson_label(following)} — {following['subject']}")
            text = (f"⏳ Через 15 минут закончится {lesson_label(lesson)}.\n"
                    f"Следующее занятие: {following_text}.")
            if room:
                text += f"\nКабинет: {room}."
            if teacher:
                text += f"\nПреподаватель: {teacher}."
        else:
            name = lesson_label(lesson) if is_practice(lesson) else f"последняя пара ({lesson['subject']})"
            text = f"⏳ Через 15 минут закончится {name}. Учебный день завершён."
        messages.append((f"reminder:{day}:{lesson['period']}:{lesson['last_period']}:{lesson['start']}:{lesson['end']}", text))
    return messages


def lesson_breaks(lessons: list[dict]) -> list[tuple[str, str]]:
    """Find gaps in teaching time, including the ten minutes inside a full pair."""
    intervals = []
    for lesson in lessons:
        halves = lesson_halves(lesson)
        cursor = lesson["start"]
        for _, _, first_end, second_start, _ in halves:
            if cursor < first_end:
                intervals.append((cursor, first_end))
            cursor = second_start
        if cursor < lesson["end"]:
            intervals.append((cursor, lesson["end"]))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [(left[1], right[0]) for left, right in zip(merged, merged[1:]) if left[1] < right[0]]


def bell_candidates(lessons: list[dict], now: datetime) -> list[tuple[str, str]]:
    day = now.date().isoformat()
    messages = []
    for start, end in lesson_breaks(lessons):
        for boundary, time_text in (("start", start), ("end", end)):
            target = datetime.combine(now.date(), datetime.strptime(time_text, "%H:%M").time(), TZ)
            if not target <= now < target + timedelta(seconds=90):
                continue
            text = (f"🔔 Звонок! Начался перерыв: {start}–{end}." if boundary == "start"
                    else f"🔔 Звонок! Перерыв закончился. Занятие начинается в {end}.")
            messages.append((f"bell:{day}:{start}:{end}:{boundary}", text))
    return messages


def queue_reminders():
    now = datetime.now(TZ)
    day = now.date().isoformat()
    with connect() as db:
        row = db.execute("SELECT payload FROM schedules WHERE day=?", (day,)).fetchone()
    if not row:
        return
    lessons = json.loads(row["payload"])["lessons"]
    for key, text in reminder_candidates(lessons, now):
        queue_message(key, text)
    for key, text in bell_candidates(lessons, now):
        queue_message(key, text)


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
            return
        if self.path == "/calendar/tick":
            seed_tomorrow_calendar()
            today = datetime.now(TZ).date().isoformat()
            with connect() as db:
                row = db.execute("""SELECT day,revision,payload FROM calendar_jobs
                                    WHERE day>=? AND revision>synced_revision ORDER BY day LIMIT 1""", (today,)).fetchone()
            self._json(200, [dict(json.loads(row["payload"]), revision=row["revision"])] if row else [])
            return
        if self.path != "/tick":
            self._json(404, {"error": "not found"})
            return
        process_one_pdf()
        queue_reminders()
        with connect() as db:
            rows = db.execute("SELECT id,chat_id,text,rich FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 20").fetchall()
        self._json(200, [dict(id=r["id"], chat_id=r["chat_id"],
                              text=r["text"] if r["rich"] else html.escape(r["text"])) for r in rows])

    def do_POST(self):
        if self.path.startswith("/calendar/ack/"):
            try:
                _, _, _, day, revision_text = self.path.split("/")
                date.fromisoformat(day)
                revision = int(revision_text)
            except (ValueError, IndexError):
                self._json(400, {"error": "bad calendar acknowledgement"})
                return
            with connect() as db:
                result = db.execute("""UPDATE calendar_jobs SET synced_revision=?
                                       WHERE day=? AND revision=?""", (revision, day, revision))
            self._json(200, {"ok": result.rowcount == 1})
            return
        if not self.path.startswith("/ack/"):
            self._json(404, {"error": "not found"})
            return
        try:
            message_id = int(self.path.rsplit("/", 1)[1])
        except ValueError:
            self._json(400, {"error": "bad id"})
            return
        with connect() as db:
            db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (datetime.now(TZ).isoformat(), message_id))
        self._json(200, {"ok": True})


async def telegram_listener():
    api_id, api_hash = os.getenv("API_ID"), os.getenv("API_HASH")
    if not api_id or not api_hash:
        logging.warning("Telegram user API credentials missing; source listener disabled")
        return
    from telethon import TelegramClient, events
    client = TelegramClient(str(DATA / "user"), int(api_id), api_hash)
    while True:
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logging.warning("Telegram user session not authorized yet")
                await client.disconnect()
                await asyncio.sleep(30)
                continue

            async def receive(message):
                filename = getattr(getattr(message, "file", None), "name", "") or ""
                if filename.lower().endswith(".pdf"):
                    with connect() as db:
                        if db.execute("SELECT 1 FROM documents WHERE message_id=?", (message.id,)).fetchone():
                            return
                    content = await message.download_media(file=bytes)
                    enqueue_pdf(message.id, filename, content)

            async for message in client.iter_messages(SOURCE, limit=25):
                await receive(message)

            async def periodic_scan():
                while client.is_connected():
                    await asyncio.sleep(30)
                    try:
                        async for message in client.iter_messages(SOURCE, limit=10):
                            await receive(message)
                    except Exception:
                        logging.exception("Telegram PDF catch-up scan failed")

            @client.on(events.NewMessage(chats=SOURCE))
            async def on_new(event):
                await receive(event.message)
            logging.info("Listening for PDFs from @%s", SOURCE)
            scan_task = asyncio.create_task(periodic_scan())
            try:
                await client.run_until_disconnected()
            finally:
                scan_task.cancel()
        except Exception:
            logging.exception("Telegram listener failed; retrying")
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(15)


def scheduler():
    last_dispatch = 0.0
    last_pending_id = None
    last_calendar_dispatch = 0.0
    last_calendar_revision = None
    while True:
        try:
            process_one_pdf()
            queue_reminders()
            with connect() as db:
                pending = db.execute("SELECT MIN(id) FROM outbox WHERE sent_at IS NULL").fetchone()[0]
            if pending and N8N_WEBHOOK_URL and (pending != last_pending_id or time.monotonic() - last_dispatch > 45):
                request = urllib.request.Request(N8N_WEBHOOK_URL, data=b"{}", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                last_dispatch = time.monotonic()
                last_pending_id = pending
            seed_tomorrow_calendar()
            today = datetime.now(TZ).date().isoformat()
            with connect() as db:
                calendar = db.execute("""SELECT day,revision FROM calendar_jobs
                                         WHERE day>=? AND revision>synced_revision ORDER BY day LIMIT 1""", (today,)).fetchone()
            pending_calendar = (calendar["day"], calendar["revision"]) if calendar else None
            if pending_calendar and N8N_CALENDAR_WEBHOOK_URL and (pending_calendar != last_calendar_revision or time.monotonic() - last_calendar_dispatch > 45):
                request = urllib.request.Request(N8N_CALENDAR_WEBHOOK_URL, data=b"{}", headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                last_calendar_dispatch = time.monotonic()
                last_calendar_revision = pending_calendar
        except Exception:
            logging.exception("Schedule tick failed; retrying")
        WAKE.wait(15)
        WAKE.clear()


if __name__ == "__main__":
    if not GROUP:
        raise RuntimeError("SCHEDULE_GROUP must be configured")
    init_db()
    threading.Thread(target=lambda: asyncio.run(telegram_listener()), daemon=True).start()
    threading.Thread(target=bot_commands, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
    logging.info("Schedule worker listening on port 8080")
    server.serve_forever()
