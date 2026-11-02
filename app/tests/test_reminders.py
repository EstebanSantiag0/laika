"""Reminders and open loops (W8.1) with a fake model and embedder: the request pattern, resolving
the time, saving at message time (and asking when no time was given), delivery with late labels and
retries, done/snooze, Telegram buttons, open loops that expire by chat days, cue notes and "asked".

Run: docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 \
       python -m tests.test_reminders
"""
import asyncio
import datetime
import re
import pathlib
import tempfile
import time
import zoneinfo

from laika.channels.telegram import reminder_buttons
from laika.db import DB
from laika.memory import Memory
from laika.nightly import Nightly
from laika.persona import Persona
from laika.recall import Recall
from laika.reminders import REQUEST, Reminders, resolve, when_label
from tests.test_consolidate import MCFG, FakeEmbedder

TZ = zoneinfo.ZoneInfo("Europe/Madrid")


class FakeLLM:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def chat(self, messages, slot, json_schema=None, **kw):
        self.calls.append((slot, messages))
        return self.answers.pop(0)


class Channel:
    def __init__(self, name, fail=False, sockets=()):
        self.name, self.fail, self.sockets, self.events = name, fail, set(sockets), []

    async def deliver(self, event):
        if self.fail:
            raise RuntimeError("network down")
        self.events.append(event)


class Engine:
    def __init__(self, channels):
        self.channels, self.published = channels, []

    async def publish(self, event):
        self.published.append(event)
        for ch in self.channels:
            try:
                await ch.deliver(event)
            except Exception:
                pass


def ask(text=None, date=None, hm=None, minutes=None, is_request=True):
    return {"is_request": is_request, "text": text or "", "date": date, "time": hm, "in_minutes": minutes}


def test_pattern_and_resolve():
    for s in ["remind me tomorrow to do that", "Recuérdame llamar a mi mamá", "no me dejes olvidar el pasaporte",
              "напомни мне завтра", "明天提醒我买菜", "can you set a reminder for 5?", "recordarme lo del banco"]:
        assert REQUEST.search(s), s
    for s in ["that reminds me of my mom", "I remember that", "recuerdo eso", "я помню"]:
        assert not REQUEST.search(s), s
    now = datetime.datetime(2026, 11, 1, 9, 45, tzinfo=TZ).timestamp()
    d = lambda ts: datetime.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M")  # noqa
    due, dflt = resolve(ask(date="2026-11-02"), now, TZ)
    assert d(due) == "2026-11-02 10:00" and dflt, "a day without a time gets the default time"
    due, dflt = resolve(ask(date="2026-11-02", hm="17:30"), now, TZ)
    assert d(due) == "2026-11-02 17:30" and not dflt
    assert d(resolve(ask(hm="08:00"), now, TZ)[0]) == "2026-11-02 08:00", "a passed time means tomorrow"
    assert d(resolve(ask(hm="21:00"), now, TZ)[0]) == "2026-11-01 21:00"
    assert d(resolve(ask(minutes=20), now, TZ)[0]) == "2026-11-01 10:05"
    assert resolve(ask(), now, TZ) is None, "no time at all: ask"
    assert resolve(ask(date="2026-10-30", hm="10:00"), now, TZ) is None, "a moment in the past: ask"
    late = datetime.datetime(2026, 11, 1, 15, 0, tzinfo=TZ).timestamp()
    assert d(resolve(ask(date="2026-11-01"), late, TZ)[0]) == "2026-11-01 16:00", "today with 10:00 gone: in 1 h"
    assert resolve(ask(date="nonsense", hm="25:00"), now, TZ) is None
    assert when_label(now + 86400, TZ, now) == "tomorrow at 09:45"
    assert when_label(now + 5 * 86400, TZ, now) == "Friday 6 Nov at 09:45"


async def test_at_message_time():
    db = DB(":memory:")
    tomorrow = (datetime.datetime.now(TZ) + datetime.timedelta(days=1)).date().isoformat()
    llm = FakeLLM([ask("Look into the logic of the automated gate", date=tomorrow),
                   ask("Call the dentist"), ask("Call the dentist", hm="23:59", is_request=False)])
    rem = Reminders(MCFG, db, llm, Engine([]))
    db.add_message("companion", "web", "Are you going to figure out the logic behind the gate?")
    m = db.add_message("user", "web", "Yes I will, remind me tomorrow to do that")
    notes = await rem.before_turn([m])
    assert len(notes) == 1 and "saved a reminder" in notes[0] and "tomorrow at 10:00" in notes[0], notes
    assert "the app chose 10:00" in notes[0]
    slot, msgs = llm.calls[0]
    assert slot == 1 and "logic behind the gate" in msgs[1]["content"], "the context is in the extraction call"
    r = rem.list()[0]
    assert r["text"] == "Look into the logic of the automated gate" and r["source_message_id"] == m["id"]
    assert r["status"] == "pending" and r["time_defaulted"] == 1

    m2 = db.add_message("user", "web", "haha ok")
    assert await rem.before_turn([m2]) == [] and len(llm.calls) == 1, "no request, no model call"

    m3 = db.add_message("user", "web", "recuérdame llamar al dentista")
    notes = await rem.before_turn([m3])
    assert "didn't say when" in notes[0] and db.get_setting("reminder_draft")["text"] == "Call the dentist"
    m4 = db.add_message("user", "web", "tonight before midnight, at 23:59")
    notes = await rem.before_turn([m4])
    assert "saved a reminder" in notes[0] and "Call the dentist" in notes[0], notes
    assert db.get_setting("reminder_draft") is None
    dentist = next(r for r in rem.list() if r["text"] == "Call the dentist")
    assert dentist["source_message_id"] == m3["id"], "the reminder points to the request"
    assert "They asked to be reminded of" in llm.calls[-1][1][0]["content"]


async def test_delivery():
    db = DB(":memory:")
    tg, web = Channel("telegram", fail=True), Channel("web")
    engine = Engine([web, tg])
    rem = Reminders(MCFG, db, FakeLLM([]), engine)
    now = time.time()
    r = rem.add("Buy milk", now - 3600, source_message_id=7)
    assert rem.due() and not await rem.deliver(rem.get(r["id"])), "Telegram down, no app open: stays pending"
    assert rem.get(r["id"])["status"] == "pending"
    first_msg = rem.get(r["id"])["message_id"]
    web.sockets.add("ws")
    assert await rem.deliver(rem.get(r["id"]))
    r = rem.get(r["id"])
    assert r["status"] == "delivered" and r["message_id"] == first_msg, "a retry reuses the same message"
    msg = db.get_message(first_msg)
    assert msg["role"] == "system" and re.match(r"⏰ Buy milk\n\(was due (today|yesterday) at", msg["text"]), msg["text"]
    # (an hour ago is "yesterday" between 00:00 and 01:00: found by the public export's tests in another time zone)
    assert any(e["type"] == "reminder" and e["reminder"]["status"] == "delivered" for e in web.events)
    assert reminder_buttons(msg) is not None
    assert not rem.due(), "delivered reminders are not sent again"

    m = db.add_message("user", "web", "thanks!")
    assert "sent them their reminder \"Buy milk\"" in rem._delivered_notes([m])[0]

    await rem.snooze(r["id"], 30)
    r = rem.get(r["id"])
    assert r["status"] == "snoozed" and r["message_id"] is None and abs(r["due_at"] - time.time() - 1800) < 5
    assert db.get_message(first_msg)["meta"]["reminder"]["answer"] == "snoozed 30 min"
    assert reminder_buttons(db.get_message(first_msg)) is None, "answered: buttons go away"
    assert not rem.due() and rem.due(time.time() + 1801), "due again after the snooze"
    await rem.done(r["id"])
    assert rem.get(r["id"])["status"] == "done" and rem.list() == [] and len(rem.list("all")) == 1
    pub = rem.public(rem.get(r["id"]))
    assert re.search(r"[+-]\d\d:\d\d$", pub["due_at"]) and pub["source_message_id"] == 7 and pub["status"] == "done"

    tg.fail = False
    web.sockets.clear()
    r2 = rem.add("On time", time.time() - 10)
    assert await rem.deliver(rem.get(r2["id"]))
    msg = db.get_message(rem.get(r2["id"])["message_id"])
    assert msg["text"] == "⏰ On time" and msg["channel"] == "telegram", "not late, sent to Telegram"


async def test_loops():
    db = DB(":memory:")
    mem = Memory(db)
    root = pathlib.Path(tempfile.mkdtemp())
    persona = Persona(MCFG, db, root)
    persona.ensure_files()
    recall = Recall(MCFG, db, mem, FakeEmbedder(), persona)
    gate = mem.add_intention({"text": "Ask if they figured out the automated gate logic", "due_from": "2026-10-25",
                              "due_to": "2026-10-25", "cue_terms": ["gate", "logic"]})
    exam = mem.add_intention({"text": "Ask how their exam went", "cue_terms": ["exam"]})

    m = db.add_message("user", "web", "the automated gate logic is clever, I figured it")
    notes = await recall.for_turn([m], batch_id=1)
    assert any("follow up on" in n and "gate" in n for n in notes), notes
    m = db.add_message("user", "web", "the automated gate logic again today")
    assert not any("follow up on" in n for n in await recall.for_turn([m], batch_id=2)), "once is enough"

    bubbles = [db.add_message("companion", "web", "Did you figure out the automated gate logic?")]
    assert await recall.mark_asked(bubbles) == [gate]
    assert [i["id"] for i in mem.intentions(("asked",))] == [gate]
    assert await recall.mark_asked([db.add_message("companion", "web", "Nice weather!")]) == []

    from laika.vault import Vault
    nightly = Nightly(MCFG, db, None, mem, Vault(root), None, persona)
    noted = time.time() - 30 * 86400
    db.conn.execute("UPDATE intentions SET created = ? WHERE id = ?", (noted, exam))
    db.conn.execute("DELETE FROM messages")
    old = mem.add_intention({"text": "Ask how the trip went", "due_from": "2026-10-20", "due_to": "2026-10-20",
                             "cue_terms": ["trip"]})
    day = lambda d: datetime.datetime(2026, 10, d, 12, tzinfo=TZ).timestamp()  # noqa
    for d in (21, 22):  # two chat days after the trip, then the server was off for a week
        db.conn.execute("INSERT INTO messages(ts, role, channel, text) VALUES (?, 'user', 'web', 'hi')", (day(d),))
    assert nightly.retire_intentions() == 1, "the asked loop becomes done"
    st = {i["id"]: i["status"] for i in mem.intentions(("open", "asked", "done", "dropped"))}
    assert st == {gate: "done", exam: "open", old: "open"}, st
    for d in (30, 31):
        db.conn.execute("INSERT INTO messages(ts, role, channel, text) VALUES (?, 'user', 'web', 'hi')", (day(d),))
    assert nightly.retire_intentions() == 1
    st = {i["id"]: i["status"] for i in mem.intentions(("open", "asked", "done", "dropped"))}
    assert st[old] == "dropped" and st[exam] == "open", "3 chat days after the trip; the undated one has 4"
    db.conn.execute("INSERT INTO messages(ts, role, channel, text) VALUES (?, 'user', 'web', 'hi')", (time.time(),))
    assert nightly.retire_intentions() == 1 and exam in [i["id"] for i in mem.intentions(("dropped",))]


async def main():
    test_pattern_and_resolve()
    await test_at_message_time()
    await test_delivery()
    await test_loops()
    print("test_reminders: all passed")


if __name__ == "__main__":
    asyncio.run(main())
