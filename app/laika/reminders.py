"""Reminders (W8.1, docs/PLAN-w8.md): "remind me tomorrow" becomes a real reminder sent at that time.

When one of his messages asks for a reminder ("remind me", "recuérdame", "напомни", "提醒我"…), one
small JSON call on the background slot reads what and when, with today's date and the last few
messages, so "remind me to do that" is understood. The time is resolved in his time zone:
- a day without a time ("mañana") gets `reminders.default_time`, and she says the time was chosen;
- a time without a day means today, or tomorrow if that time has passed;
- no time at all saves nothing yet: she asks, and his next message is read for the time.
The turn's note tells her what was saved, so her confirmation matches the app.

A scheduler in the app delivers due reminders as a ⏰ system message (Telegram, with "Done" and
"Snooze" buttons, and the web app) plus a WebSocket event {"type": "reminder", ...}. A reminder the
server missed while it was down goes out late on boot, marked as late; nothing is ever dropped. If
no channel can be reached it stays pending and is tried again. Status: pending → delivered → done,
or snoozed (a new time, delivered again).
"""
import asyncio
import datetime
import json
import logging
import re
import time
import zoneinfo

from .config import personal, user_name

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    due_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    areas TEXT NOT NULL DEFAULT '[]',
    source_message_id INTEGER,
    channel TEXT,
    time_defaulted INTEGER NOT NULL DEFAULT 0,
    message_id INTEGER,
    created REAL NOT NULL,
    delivered_at REAL,
    done_at REAL,
    snoozes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
"""
STATUSES = ("pending", "delivered", "snoozed", "done")
REQUEST = re.compile(
    r"\bremind\s+me\b|\breminder\b|\bdon'?t\s+let\s+me\s+forget\b|"
    r"\brecu[eé]rd[ae]me\b|\brecord[aá]rme\b|\bacu[eé]rd[ae]me\b|\brecordatorio\b|\bno\s+me\s+dejes\s+olvidar\b|"
    r"\bav[ií]same\b|"
    r"напомни|напоминани|не\s+дай\s+(?:мне\s+)?забыть|"
    r"提醒|别让我忘", re.I)
EXTRACT = """You read reminder requests for a chat app. Now it is {now} ({tz}). {user} is chatting \
with his friend Laika. Read the last message from {user} and say whether he is asking to be reminded \
of something at a later time, and if so, what and when.
- is_request: false if he is not asking for a reminder (e.g. "that reminds me of my mom").
- text: what to remind him of, short (at most 12 words), in the language he wrote in, written as a \
note to him ("Llamar al dentista", "Renew the passport"). Replace "that" or \
"eso" with what it refers to in the conversation.
- date: the day as YYYY-MM-DD if he said one ("tomorrow", "el viernes", "на следующей неделе в среду"), \
else null.
- time: the time as HH:MM (24 h) if he said one ("at 5" in the afternoon is 17:00), else null.
- in_minutes: if he said a delay ("in 20 minutes", "en una hora", "через час"), that many minutes, else null.
- areas: the parts of his life it belongs to, at most 2 of: {areas}. An empty list if none fits."""
FOLLOW_UP = """He asked to be reminded of: "{text}". Laika asked him when. Read his last message for \
the day and time (keep the text unless he changed it)."""
SCHEMA_JSON = {"type": "object", "properties": {
    "is_request": {"type": "boolean"}, "text": {"type": "string"},
    "date": {"type": ["string", "null"]}, "time": {"type": ["string", "null"]},
    "in_minutes": {"type": ["integer", "null"]},
    "areas": {"type": "array", "maxItems": 2, "items": {"type": "string"}}},
    "required": ["is_request", "text", "date", "time", "in_minutes", "areas"]}
CONTEXT_MSGS = 8
DRAFT_TTL_S = 2 * 3600
LATE_S = 300
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def when_label(ts, tz, now=None):
    """'today at 17:00', 'tomorrow at 10:00', 'Friday 6 Nov at 09:30' (absolute beyond tomorrow)."""
    d = datetime.datetime.fromtimestamp(ts, tz)
    today = datetime.datetime.fromtimestamp(now or time.time(), tz).date()
    days = (d.date() - today).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else "yesterday" if days == -1 else \
        f"{WEEKDAYS[d.weekday()]} {d.day} {d:%b}"
    return f"{day} at {d:%H:%M}"


def resolve(out, now_ts, tz, default_time="10:00"):
    """(due_ts, time_defaulted) from the extractor's answer, or None when it gives no usable time."""
    now = datetime.datetime.fromtimestamp(now_ts, tz)
    if out.get("in_minutes"):
        return now_ts + max(1, int(out["in_minutes"])) * 60, False
    day = _date(out.get("date"))
    hm = _time(out.get("time"))
    if day is None and hm is None:
        return None
    defaulted = hm is None
    hm = hm or _time(default_time) or (10, 0)
    if day is None:
        due = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        if due <= now:
            due += datetime.timedelta(days=1)
        return due.timestamp(), False
    due = datetime.datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
    if due <= now:
        if not defaulted:
            return None  # a moment in the past: better to ask
        due = now + datetime.timedelta(hours=1)  # "today" with the default hour already gone
        due = due.replace(second=0, microsecond=0)
    return due.timestamp(), defaulted


def _date(s):
    try:
        return datetime.date.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def _time(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


class Reminders:
    def __init__(self, cfg, db, llm, engine):
        self.cfg, self.db, self.llm, self.engine = cfg, db, llm, engine
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        r = cfg.get("reminders") or {}
        self.default_time = r.get("default_time", "10:00")
        self.check_s = r.get("check_s", 20)
        self.snooze_min = r.get("snooze_min", 60)
        self.slot = cfg["llm"]["background_slot"]
        self.areas = None  # W8.3: set in main.py (areas.py)
        self.db.conn.executescript(SCHEMA)
        if "updated" not in {r["name"] for r in self.db.conn.execute("PRAGMA table_info(reminders)")}:
            self.db.conn.execute("ALTER TABLE reminders ADD COLUMN updated REAL")  # SB7b: stale-edit check

    # storage ---------------------------------------------------------------------------------------
    def add(self, text, due_at, source_message_id=None, channel=None, time_defaulted=False, areas=()):
        cur = self.db.conn.execute(
            "INSERT INTO reminders(text, due_at, source_message_id, channel, time_defaulted, areas, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (text, due_at, source_message_id, channel, int(time_defaulted), json.dumps(list(areas)), time.time()))
        log.info("reminder %s saved for %s", cur.lastrowid, when_label(due_at, self.tz))
        return self.get(cur.lastrowid)

    def get(self, reminder_id):
        row = self.db.conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _row(row) if row else None

    def list(self, status="pending"):
        """Pending means not finished yet: pending, delivered (not marked done) or snoozed."""
        if status == "all":
            rows = self.db.conn.execute("SELECT * FROM reminders WHERE status != 'trashed' ORDER BY due_at")
        else:
            rows = self.db.conn.execute("SELECT * FROM reminders WHERE status IN ('pending', 'delivered', 'snoozed') "
                                        "ORDER BY due_at")
        return [_row(r) for r in rows]

    def due(self, now=None):
        return [_row(r) for r in self.db.conn.execute(
            "SELECT * FROM reminders WHERE status IN ('pending', 'snoozed') AND due_at <= ? ORDER BY due_at",
            (now or time.time(),))]

    def public(self, r):
        """The API shape (docs/API.md)."""
        return {"id": r["id"], "text": r["text"], "due_at": _iso(r["due_at"], self.tz), "status": r["status"],
                "areas": r["areas"], "source_message_id": r["source_message_id"],
                "time_defaulted": bool(r["time_defaulted"]), "created_at": _iso(r["created"], self.tz),
                "delivered_at": _iso(r["delivered_at"], self.tz), "done_at": _iso(r["done_at"], self.tz),
                "snoozes": r["snoozes"]}

    async def done(self, reminder_id):
        r = self.get(reminder_id)
        if not r:
            return None
        if r["status"] != "done":
            self.db.conn.execute("UPDATE reminders SET status = 'done', done_at = ?, updated = ? WHERE id = ?",
                                 (time.time(), time.time(), reminder_id))
        return await self._changed(reminder_id, "done")

    async def snooze(self, reminder_id, minutes=None):
        r = self.get(reminder_id)
        if not r or r["status"] == "done":
            return None
        minutes = max(1, min(int(minutes or self.snooze_min), 60 * 24 * 30))
        self.db.conn.execute("UPDATE reminders SET status = 'snoozed', due_at = ?, snoozes = snoozes + 1, "
                             "message_id = NULL, updated = ? WHERE id = ?", (time.time() + minutes * 60, time.time(), reminder_id))
        return await self._changed(reminder_id, f"snoozed {minutes} min")

    async def _changed(self, reminder_id, answer):
        """Tell the clients, and take the buttons off the delivered message."""
        r = self.get(reminder_id)
        msg_id = self.db.conn.execute("SELECT id FROM messages WHERE json_extract(meta, '$.reminder.id') = ? "
                                      "ORDER BY id DESC LIMIT 1", (reminder_id,)).fetchone()
        if msg_id and (msg := self.db.get_message(msg_id[0])) and not msg["meta"]["reminder"].get("answer"):
            self.db.set_meta(msg["id"], reminder={**msg["meta"]["reminder"], "answer": answer})
            await self.engine.publish({"type": "message", "message": self.db.get_message(msg["id"]),
                                       "origin": msg["channel"], "update": True})
        await self.engine.publish({"type": "reminder", "reminder": self.public(r), "origin": "app"})
        return r

    # at message time -------------------------------------------------------------------------------
    async def before_turn(self, batch, batch_id=None):
        """Notes for this turn: a reminder just saved (or a question about when), and reminders
        that went out since her last reply."""
        notes = self._delivered_notes(batch)
        text = "\n".join(m["text"] for m in batch)
        draft = self.db.get_setting("reminder_draft")
        if draft and time.time() - draft.get("ts", 0) > DRAFT_TTL_S:
            self.db.set_setting("reminder_draft", None)
            draft = None
        if not REQUEST.search(text) and not draft:
            return notes
        try:
            out = await self._extract(batch, draft)
        except Exception as e:
            log.warning("reminder extraction failed: %s", e)
            if REQUEST.search(text):
                notes.append(personal("he may be asking for a reminder, but the app couldn't save it right now: tell "
                                      "him you can't set it this time, don't promise to remind him", self.cfg))
            return notes
        if not out.get("is_request") and not draft:
            return notes
        what = (out.get("text") or (draft or {}).get("text") or "").strip()[:200]
        if not what:
            return notes
        resolved = resolve(out, time.time(), self.tz, self.default_time)
        if resolved is None:
            if draft:  # asked once already: let it go rather than nag
                self.db.set_setting("reminder_draft", None)
                return notes
            self.db.set_setting("reminder_draft", {"text": what, "source": batch[-1]["id"], "ts": time.time()})
            notes.append(personal("he wants a reminder (\"{what}\") but didn't say when: ask him what day and time; "
                                  "the app will save it from his answer", self.cfg).format(what=what))
            return notes
        due, defaulted = resolved
        source = (draft or {}).get("source") or batch[-1]["id"]
        self.db.set_setting("reminder_draft", None)
        areas = self.areas.clean(out.get("areas")) if self.areas else []
        r = self.add(what, due, source_message_id=source, channel=batch[-1]["channel"], time_defaulted=defaulted,
                     areas=areas)
        await self.engine.publish({"type": "reminder", "reminder": self.public(r), "origin": "app"})
        chosen = personal(" (he gave no time, so the app chose {t}; he can tell you another)", self.cfg).format(
            t=self.default_time) if defaulted else ""
        notes.append(personal("the app saved a reminder for him: \"{what}\", {when}{chosen}. Confirm it in a few words "
                              "with the day and time; it will reach him here and on Telegram", self.cfg).format(
            what=what, when=when_label(due, self.tz), chosen=chosen))
        return notes

    async def _extract(self, batch, draft):
        now = datetime.datetime.now(self.tz).strftime("%A %d %B %Y, %H:%M")
        rows = self.db.conn.execute(
            "SELECT role, text FROM messages WHERE role IN ('user', 'companion') AND id < ? ORDER BY id DESC LIMIT ?",
            (batch[0]["id"], CONTEXT_MSGS)).fetchall()
        name = user_name(self.cfg)
        lines = [f"{name if r['role'] == 'user' else 'Laika'}: {r['text'][:400]}" for r in reversed(rows)]
        lines += [f"{name}: {m['text'][:1000]}" for m in batch]
        system = personal(EXTRACT, self.cfg).format(now=now, tz=self.cfg["memory"]["tz"],
                                areas=self.areas.prompt_list() if self.areas else "(none)")
        if draft:
            system += "\n" + personal(FOLLOW_UP, self.cfg).format(text=draft["text"])
        schema = SCHEMA_JSON
        if self.areas:
            schema = {**SCHEMA_JSON, "properties": {**SCHEMA_JSON["properties"], "areas": self.areas.schema()}}
        return await self.llm.chat([{"role": "system", "content": system},
                                    {"role": "user", "content": "\n".join(lines)}],
                                   self.slot, json_schema=schema, temperature=0.0, max_tokens=120)

    def _delivered_notes(self, batch):
        """Reminders that went out after her last reply, so she knows about them."""
        row = self.db.conn.execute("SELECT MAX(ts) FROM messages WHERE role = 'companion' AND id < ?",
                                   (batch[0]["id"],)).fetchone()
        since = row[0] or 0
        sent = self.db.conn.execute("SELECT text, delivered_at FROM reminders WHERE delivered_at > ? "
                                    "ORDER BY delivered_at", (since,)).fetchall()
        return [personal("the app sent him his reminder ", self.cfg) + f"\"{r['text']}\" "
                f"({datetime.datetime.fromtimestamp(r['delivered_at'], self.tz):%H:%M})" for r in sent]

    # scheduler -------------------------------------------------------------------------------------
    async def loop(self):
        """Deliver what is due; the first pass right at start-up sends what was missed while down."""
        while True:
            try:
                for r in self.due():
                    await self.deliver(r)
            except Exception:
                log.exception("reminder scheduler")
            await asyncio.sleep(self.check_s)

    def _reachable(self):
        names = set()
        for ch in self.engine.channels:
            if ch.name == "web" and getattr(ch, "sockets", None):
                names.add("web")
            elif ch.name == "telegram":
                names.add("telegram")
        return names

    async def deliver(self, r, now=None):
        now = now or time.time()
        reachable = self._reachable()
        if not reachable:
            return False  # nobody to tell: try again on the next pass
        origin = "telegram" if "telegram" in reachable else "web"
        late = now - r["due_at"] > LATE_S
        text = f"⏰ {r['text']}" + (f"\n(was due {when_label(r['due_at'], self.tz, now)})" if late else "")
        msg = self.db.get_message(r["message_id"]) if r["message_id"] else None
        if msg is None:
            msg = self.db.add_message("system", origin, text, meta={"reminder": {"id": r["id"]}})
            self.db.conn.execute("UPDATE reminders SET message_id = ? WHERE id = ?", (msg["id"], r["id"]))
        event = {"type": "message", "message": msg, "origin": origin}
        delivered = set()
        for ch in self.engine.channels:
            try:
                await ch.deliver(event)
                if ch.name in reachable:
                    delivered.add(ch.name)
            except Exception as e:
                log.warning("reminder %s: %s failed: %s", r["id"], ch.name, e)
        if not delivered:
            return False
        self.db.conn.execute("UPDATE reminders SET status = 'delivered', delivered_at = ? WHERE id = ?",
                             (time.time(), r["id"]))
        log.info("reminder %s delivered via %s%s", r["id"], ",".join(sorted(delivered)), " (late)" if late else "")
        await self.engine.publish({"type": "reminder", "reminder": self.public(self.get(r["id"])), "origin": "app"})
        return True


def _iso(ts, tz):
    return datetime.datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds") if ts else None


def _row(row):
    r = dict(row)
    r["areas"] = json.loads(r.get("areas") or "[]")
    return r
