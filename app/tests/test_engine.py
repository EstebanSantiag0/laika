"""Engine tests with a fake model: batching, typing holds, interruptions, bubble splitting, commands.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_engine
"""
import asyncio
import pathlib
import tempfile

from laika.conversation import Engine
from laika.db import DB
from laika.persona import Persona

CFG = {
    "llm": {"conversation_slot": 0, "background_slot": 1},
    "batching": {"web_quiet_s": 0.3, "typing_idle_s": 0.5, "draft_hold_s": 0.8, "voice_quiet_s": 0.1,
                 "telegram_quiet_s": 0.3, "telegram_unfinished_s": 1.0, "burst_factor": 1.5, "burst_max_s": 2,
                 "max_wait_s": 3, "max_hold_s": 5},
    "bubbles": {"max": 4, "ms_per_char": 0, "min_delay_s": 0, "max_delay_s": 0},
    "language": {"default": "en", "native": "es", "pinyin": True, "corrections": "on"},
    "history": {"budget_tokens": 6000, "keep_after_trim": 0.5},
    "memory": {"tz": "Europe/Madrid"},
    "user": {"name": "Dasha", "pronouns": "they"},
}


class FakeLLM:
    def __init__(self, reply, delay=0.05):
        self.reply, self.delay, self.prompts, self.chats = reply, delay, [], []

    async def chat(self, messages, slot, json_schema=None, **kw):
        self.chats.append((slot, messages))
        return "- Dasha likes theater" if slot == 1 else "ok"

    async def warm(self, messages, slot):
        self.chats.append((slot, messages))

    async def stream_chat(self, messages, slot, **kw):
        self.prompts.append(messages)
        for i in range(0, len(self.reply), 5):
            await asyncio.sleep(self.delay)
            yield self.reply[i:i + 5]


class Recorder:
    name = "rec"

    def __init__(self):
        self.events = []

    async def deliver(self, event):
        self.events.append(event)

    def bubbles(self):
        return [e["message"]["text"] for e in self.events
                if e["type"] == "message" and e["message"]["role"] == "companion"]


def make(reply, delay=0.05):
    vault = pathlib.Path(tempfile.mkdtemp())
    db = DB(":memory:")
    persona = Persona(CFG, db, vault)
    persona.ensure_files()
    llm = FakeLLM(reply, delay)
    engine = Engine(CFG, db, llm, persona)
    rec = Recorder()
    engine.channels.append(rec)
    return engine, llm, rec, db


async def settle(engine):
    for _ in range(200):
        await asyncio.sleep(0.05)
        busy = (engine.timer and not engine.timer.done()) or (engine.turn and not engine.turn.done())
        if not busy and not engine.pending:
            return
    raise AssertionError("engine did not settle")


async def test_batching_and_bubbles():
    engine, llm, rec, _ = make("Hi there!\n\nHow was your day?\n\nTell me everything.")
    await engine.receive("web", "hello")
    await engine.receive("web", "I have news")
    await settle(engine)
    assert len(llm.prompts) == 1, llm.prompts
    final = llm.prompts[0][-1]["content"]
    assert "hello\nI have news" in final, final
    assert rec.bubbles() == ["Hi there!", "How was your day?", "Tell me everything."], rec.bubbles()
    kinds = [e["type"] for e in rec.events]
    first_bubble = next(i for i, e in enumerate(rec.events)
                        if e["type"] == "message" and e["message"]["role"] == "companion")
    assert "typing" in kinds[:first_bubble], kinds
    assert kinds.index("typing") > kinds.index("read"), "typing must start after reading, when text arrives"
    assert [k for k in kinds if k.startswith("typing")][-1] == "typing_stop", kinds
    print("ok batching_and_bubbles")


async def test_restart_before_first_bubble():
    engine, llm, rec, _ = make("First part of a long answer\n\nsecond part", delay=0.2)
    await engine.receive("web", "one")
    await asyncio.sleep(0.45)  # turn has started but no bubble is out yet
    assert engine.turn and not engine.turn.done()
    await engine.receive("web", "two")
    await settle(engine)
    assert len(llm.prompts) == 2, len(llm.prompts)
    assert "one\ntwo" in llm.prompts[-1][-1]["content"]
    assert rec.bubbles() == ["First part of a long answer", "second part"], rec.bubbles()
    print("ok restart_before_first_bubble")


async def test_max_bubbles():
    engine, _, rec, _ = make("a1\n\nb2\n\nc3\n\nd4\n\ne5\n\nf6")
    await engine.receive("web", "hi")
    await settle(engine)
    assert rec.bubbles() == ["a1", "b2", "c3", "d4\n\ne5\n\nf6"], rec.bubbles()
    print("ok max_bubbles")


async def test_command_and_history():
    engine, llm, rec, db = make("Sure!")
    await engine.receive("web", "/lang ru")
    assert db.get_setting("practice_lang") == "ru"
    assert not engine.pending
    await engine.receive("web", "Привет")
    await settle(engine)
    await engine.receive("web", "Как дела?")
    await settle(engine)
    msgs = llm.prompts[-1]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"], roles
    assert "practice language: Russian" in msgs[-1]["content"], msgs[-1]["content"]
    assert "Russian (their level:" in msgs[0]["content"] and "Chinese (their level:" in msgs[0]["content"]
    # exact replay: the earlier user turn comes back with the note it was sent with, the reply as generated
    first_prompt_turn = llm.prompts[0][-1]["content"]
    assert msgs[1]["content"] == first_prompt_turn, (msgs[1]["content"], first_prompt_turn)
    assert msgs[2]["content"] == "Sure!"
    print("ok command_and_history")


async def test_compaction():
    from laika.compaction import Compactor

    engine, llm, rec, db = make("Sure, tell me more about it, I'm curious.")
    engine.cfg = {**CFG, "history": {"budget_tokens": 40, "keep_after_trim": 0.5}}
    engine.on_trim.append(Compactor(engine.cfg, db, llm, engine).on_trim)
    for text in ["I went to the theater yesterday", "the movie was very sad", "and my cousin cried a lot"]:
        await engine.receive("web", text)
        await settle(engine)
    await asyncio.sleep(0.3)  # background hooks
    assert db.get_setting("history_start_id", 0) > 0, "history was not trimmed"
    assert db.get_setting("running_summary") == "- Dasha likes theater"
    assert any(slot == 0 for slot, _ in llm.chats), "cache was not warmed on the conversation slot"
    await engine.receive("web", "what do you think?")
    await settle(engine)
    assert "Earlier in this conversation" in llm.prompts[-1][0]["content"]
    print("ok compaction")


async def keep_typing(engine, seconds, draft=True):
    """Simulate Dasha typing: a keystroke event every 0.2 s."""
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        await engine.typing(active=True, draft=draft)
        await asyncio.sleep(0.2)


async def test_waits_while_typing():
    engine, llm, rec, _ = make("Okay!")
    await engine.receive("web", "one")
    await keep_typing(engine, 1.5)  # far longer than the 0.3 s quiet gap
    assert not llm.prompts, "she answered while they were still typing"
    await engine.typing(active=False)  # they cleared the box
    await settle(engine)
    assert len(llm.prompts) == 1 and "one" in llm.prompts[0][-1]["content"]
    print("ok waits_while_typing")


async def test_draft_hold_expires():
    engine, llm, rec, _ = make("Okay!")
    await engine.receive("web", "one")
    await engine.typing(active=True, draft=True)  # typed something, then stopped to think
    await asyncio.sleep(0.5)
    assert not llm.prompts, "she answered while their draft was still being written"
    await settle(engine)  # draft_hold_s (0.8 s) runs out: she answers
    assert len(llm.prompts) == 1
    print("ok draft_hold_expires")


async def test_bubble_held_while_typing():
    engine, llm, rec, _ = make("First bubble here\n\nSecond bubble\n\nThird bubble", delay=0.02)
    await engine.receive("web", "hi")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if rec.bubbles():
            break
    assert rec.bubbles() == ["First bubble here"], rec.bubbles()
    await keep_typing(engine, 1.2)
    assert rec.bubbles() == ["First bubble here"], f"bubbles went out while they typed: {rec.bubbles()}"
    await engine.typing(active=False)
    await settle(engine)
    assert rec.bubbles() == ["First bubble here", "Second bubble", "Third bubble"], rec.bubbles()
    print("ok bubble_held_while_typing")


async def test_message_after_first_bubble_interrupts():
    engine, llm, rec, db = make("First bubble here\n\nSecond bubble\n\nThird bubble", delay=0.02)
    await engine.receive("web", "one")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if rec.bubbles():
            break
    await engine.typing(active=True, draft=True)  # they start typing: the rest is held...
    await asyncio.sleep(0.3)
    await engine.receive("web", "two")  # ...and they send: the rest is dropped
    await settle(engine)
    statuses = [r[0] for r in db.conn.execute("SELECT status FROM batches ORDER BY id")]
    assert statuses == ["interrupted", "done"], statuses
    assert len(llm.prompts) == 2
    final = llm.prompts[-1][-1]["content"]
    assert "two" in final and "one" not in final, final
    assert "still replying" in final, final
    assert rec.bubbles() == ["First bubble here", "First bubble here", "Second bubble", "Third bubble"], rec.bubbles()
    history = [m["content"] for m in llm.prompts[-1] if m["role"] == "assistant"]
    assert history == ["First bubble here"], history
    print("ok message_after_first_bubble_interrupts")


async def test_voice_note_holds_turn():
    engine, llm, rec, _ = make("Okay!")
    await engine.receive("web", "listen to this")
    async with engine.incoming():  # a voice note is being transcribed
        await asyncio.sleep(1.0)
        assert not llm.prompts, "she answered while their voice note was still being transcribed"
        await engine.receive("web", "(the transcript)", voice=True)
    await settle(engine)
    assert len(llm.prompts) == 1 and "listen to this" in llm.prompts[0][-1]["content"]
    assert "(the transcript)" in llm.prompts[0][-1]["content"]
    print("ok voice_note_holds_turn")


async def test_telegram_patience():
    from laika.conversation import looks_unfinished

    for text in ["so yesterday I went to the market and", "hey", "Oye,", "I was thinking...", "и",
                 "我今天去了商店，然后", "pero"]:
        assert looks_unfinished(text), text
    for text in ["I went to the market.", "what do you think?", "haha", "Привет! Как дела?", "我很好。", "ok"]:
        assert not looks_unfinished(text), text
    engine, llm, rec, _ = make("Okay!")
    msg = await engine.receive("telegram", "so yesterday I went to the market and")
    assert engine.deadline - asyncio.get_running_loop().time() > 0.6, "unfinished message got the short gap"
    await settle(engine)
    msg = await engine.receive("telegram", "it was closed.")
    assert abs(engine._quiet("telegram", False, msg) - 0.3) < 1e-6
    print("ok telegram_patience")


async def test_mic_marker_and_voice_note():
    engine, llm, rec, _ = make("Haha!\n\n🎤 Привет\n\nok, now text again")
    spoken = []

    async def hook(msg, origin, voice):
        spoken.append((msg["text"], voice))
    engine.on_bubble.append(hook)
    await engine.receive("web", "how do you say hi in Russian?")
    await settle(engine)
    assert spoken == [("Haha!", False), ("Привет", True), ("ok, now text again", False)], spoken
    engine.voice_allowed = lambda: False  # voice mode "never": the marker is dropped, nothing is spoken
    spoken.clear()
    await engine.receive("web", "again?")
    await settle(engine)
    assert spoken == [("Haha!", False), ("Привет", False), ("ok, now text again", False)], spoken
    engine.voice_policy = lambda batch: True  # a voice turn: she is told her reply will be spoken
    await engine.receive("web", "tell me more")
    await settle(engine)
    assert "sent as voice notes" in llm.prompts[-1][-1]["content"], llm.prompts[-1][-1]["content"]
    print("ok mic_marker_and_voice_note")


def test_voice_requests_and_speech_text():
    from laika.voice import VOICE_REQUEST, speech_text

    for text in ["send me a voice note", "Can you send me an audio?", "how do you pronounce щ?", "say it out loud",
                 "mándame un audio", "¿cómo se pronuncia 谢谢?", "запиши голосовое", "скажи вслух", "给我发个语音",
                 "这个字怎么读?",
                 # W6: msg 178 verbatim, and other ways of asking to hear her
                 "我很好,你呢？（你今天怎么样 new vocabulary for me, could you say that using you voice?）",
                 "say it with your voice", "can you read this in your voice?", "repeat that one using your voice",
                 "answer me using your voice", "send it in a voice note", "dilo con tu voz", "Dilo en ruso",
                 "pronúncialo por favor", "pronunciame esto", "скажи это своим голосом", "ответь голосом",
                 "用你的声音说", "你可以说一下吗", "说给我听"]:
        assert VOICE_REQUEST.search(text), text
    for text in ["the audio quality in that movie was bad", "I love your voice", "I went to the market",
                 "привет, как дела?", "我很好", "Your voice is beautiful, you know?", "I said it in my own words",
                 "what a dilemma", "la pronunciación es difícil", "у тебя красивый голос", "你的声音很好听"]:
        assert not VOICE_REQUEST.search(text), text
    assert speech_text("Привет! Кошка (cat) 😊", "ru") == "Привет! Кошка"
    assert speech_text("我在听音乐 (I'm listening to music)。你呢？", "zh") == "我在听音乐。你呢？"
    assert speech_text("That's **so** cool (really) 🎉", "en") == "That's so cool (really)"
    assert speech_text("😊", "en") == ""
    # W6: pinyin and English glosses are captions only; full-width parentheses too
    assert speech_text("And of course: 你今天怎么样? (Nǐ jīntiān zěnmeyàng? - How are you today?)", "zh") \
        == "And of course: 你今天怎么样?"
    assert speech_text("你好（nǐ hǎo）！", "zh") == "你好！"
    assert speech_text("(Hint: Student is 学生 xuésheng)", "zh") == "(Hint: Student is 学生 xuésheng)"  # nothing left
    print("ok voice_requests_and_speech_text")


def test_bubble_lang_and_pinyin():
    from laika import lang
    from laika.tutor import pinyin_line

    # W6: her mixed bubbles from 2026-10-18 (msgs 179, 214, 240, 252, 263) were stored as en/es and read in English
    mixed = ["我也很好 (Wǒ yě hěn hǎo - I am also very good), thank you!",
             "你叫什么名字? (Nǐ jiào shénme míngzi? - What is your name?)",
             "你是我的朋友 (Nǐ shì wǒ de péngyou - You are my friend).",
             "In Chinese, that would be: 我在学习计算机工程 (Wǒ zài xuéxí jìsuànjī gōngchéng).",
             "我叫 Laika (Wǒ jiào Laika)."]
    for text in mixed:
        assert lang.bubble_lang(text, "zh") == "zh", text
    assert lang.detect(mixed[1]) == "en", "pinyin tone marks must not count as Spanish"
    assert lang.bubble_lang("你今天怎么样？(Nǐ jīntiān zěnmeyàng - How are you today?)", "en") == "zh"
    assert lang.bubble_lang("Всё хорошо (all good), спасибо (thanks)! It means you're fine", "ru") == "ru"
    for text, code in [("You're doing great!", "en"), ("¡Qué bien, me alegro mucho!", "es"), ("Привет", "ru"),
                       ('The "觉" (jué) you used is a bit different', "en"), ("That's so cool (really)", "en")]:
        for practice in ("en", "ru", "zh"):
            if practice != code:
                assert lang.bubble_lang(text, practice) == code, (text, practice)
    assert pinyin_line(mixed[0]) == "wǒ yě hěn hǎo"
    assert pinyin_line(mixed[1]) == "nǐ jiào shén me míng zì?"
    assert pinyin_line(mixed[4]) == "wǒ jiào Laika."
    assert pinyin_line("In Chinese, that would be: 我是 Dasha, 你呢? (Wǒ shì Dasha, nǐ ne?)") \
        == "wǒ shì Dasha, nǐ ne?"
    assert pinyin_line('"计算机" (jìsuànjī) is computer, but "工厂" (gōngchǎng) is factory.') == "jì suàn jī · gōng chǎng"
    assert pinyin_line("你是墨西哥人 (You are Spanish)，很有意思 (very interesting)! 🇲🇽") == "nǐ shì mò xī gē rén, hěn yǒu yì si!"
    print("ok bubble_lang_and_pinyin")


async def test_mixed_chinese_voice_bubble():
    from laika.tutor import Tutor

    engine, llm, rec, db = make("🎤 你今天怎么样？(Nǐ jīntiān zěnmeyàng? - How are you today?)\n\n"
                                "That's the one you'll hear all the time")
    engine.annotate = Tutor(CFG, db, llm, engine).annotate
    db.set_setting("practice_lang", "zh")
    spoken = []

    async def hook(msg, origin, voice):
        spoken.append((msg["text"], msg["lang"], voice))
    engine.on_bubble.append(hook)
    await engine.receive("web", "how do you say how are you today?")
    await settle(engine)
    phrase = "你今天怎么样？(Nǐ jīntiān zěnmeyàng? - How are you today?)"
    # her own 🎤 choice: only the phrase is a voice note, and it is Chinese (read in Chinese, gloss not spoken)
    assert spoken == [(phrase, "zh", True), ("That's the one you'll hear all the time", "en", False)], spoken
    first = next(e["message"] for e in rec.events if e["type"] == "message" and e["message"]["role"] == "companion")
    assert first["meta"] == {"pinyin": "nǐ jīn tiān zěn me yàng?"}, first["meta"]
    engine.voice_policy = lambda batch: True  # they asked to hear her (VOICE_REQUEST): the whole reply is spoken
    spoken.clear()
    await engine.receive("web", "say it with your voice")
    await settle(engine)
    assert spoken == [(phrase, "zh", True), ("That's the one you'll hear all the time", "en", True)], spoken
    print("ok mixed_chinese_voice_bubble")


async def test_language_offer():
    """W5 (D17): writing in another practice language only offers a switch; nothing changes until they say yes."""
    engine, llm, rec, db = make("Okay!")
    offers = lambda: [m for m in db.history() if m["meta"].get("offer")]  # noqa: E731
    await engine.receive("web", "Привет, как у тебя дела сегодня?")
    await settle(engine)
    assert db.get_setting("practice_lang", "en") == "en", "the language changed without asking"
    [offer] = offers()
    assert offer["text"] == "Switch to Русский?" and offer["meta"]["offer"] == {"practice_lang": "ru"}, offer
    assert "they wrote in Russian; the practice language is still English (the app asked them whether to switch)" \
        in llm.prompts[-1][-1]["content"]
    await engine.receive("web", "Я сегодня много работал")
    await settle(engine)
    assert len(offers()) == 1, "a second Russian batch made another offer"
    await engine.receive("web", "what does привет mean? I keep seeing it")  # mixed: no offer
    await settle(engine)
    assert len(offers()) == 1
    rec.events.clear()
    assert await engine.answer_offer(offer["id"], True, "web")
    assert db.get_setting("practice_lang") == "ru"
    assert db.get_message(offer["id"])["meta"]["offer"]["answer"] == "switched"
    kinds = [(e["type"], e.get("update", False)) for e in rec.events]
    assert ("settings", False) in kinds and ("message", True) in kinds, kinds
    assert any(e["type"] == "message" and e["message"]["text"] == "✓ practice_lang = Русский" for e in rec.events)
    assert not await engine.answer_offer(offer["id"], True, "web"), "an answered offer was answered again"

    engine, llm, rec, db = make("Okay!")  # "Not now": no new offer for that language during the snooze
    await engine.receive("web", "你好！我今天很累，想休息")
    await settle(engine)
    [offer] = offers()
    assert offer["meta"]["offer"]["practice_lang"] == "zh"
    assert await engine.answer_offer(offer["id"], False, "web")
    assert db.get_setting("practice_lang", "en") == "en"
    assert db.get_message(offer["id"])["meta"]["offer"]["answer"] == "not_now"
    await engine.receive("web", "我想去吃饭了，你呢？")
    await settle(engine)
    assert len(offers()) == 1, "an offer came back during the snooze"
    assert "the practice language is still English (they chose to keep it for now)" in llm.prompts[-1][-1]["content"]
    await engine.receive("web", "Привет, как у тебя дела?")  # another language is not snoozed
    await settle(engine)
    assert [o["meta"]["offer"]["practice_lang"] for o in offers()] == ["zh", "ru"]
    await engine.receive("web", "/lang en")  # any switch closes the open offer
    assert offers()[-1]["meta"]["offer"]["answer"] == "dismissed"
    print("ok language_offer")


async def main():
    await test_batching_and_bubbles()
    await test_restart_before_first_bubble()
    await test_max_bubbles()
    await test_command_and_history()
    await test_compaction()
    await test_waits_while_typing()
    await test_draft_hold_expires()
    await test_bubble_held_while_typing()
    await test_message_after_first_bubble_interrupts()
    await test_voice_note_holds_turn()
    await test_telegram_patience()
    await test_mic_marker_and_voice_note()
    test_voice_requests_and_speech_text()
    test_bubble_lang_and_pinyin()
    await test_mixed_chinese_voice_bubble()
    await test_language_offer()
    print("all engine tests passed")


if __name__ == "__main__":
    asyncio.run(main())
