"""Vocabulary and difficulty (P4.4): the starting point per level, Chinese compounds, evidence from their
messages and corrections, coverage per book and chapter, glosses with readings and book examples, notes.

Needs wordfreq (in the image since P4.4). Run:
docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_vocab
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import library  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.persona import Persona  # noqa: E402
from laika.vault import Vault  # noqa: E402
from laika.vocab import Vocab, baseline, hsk_levels, label, level_size  # noqa: E402
from tests.test_engine import CFG  # noqa: E402
from tests.test_library import FakeEmbedder, FakeEngine  # noqa: E402

EASY_ZH = """第一章 我的家
我是学生。我有一个朋友。我们都喜欢看书。我家有三个人：爸爸、妈妈和我。
今天我们去学校。老师说：“你们好！”我们很高兴。
第二章 小狗
我家有一只小狗。小狗很小，很可爱。我每天和小狗一起玩。
"""
HARD_ZH = """第一章 秋夜
在我的后园，可以看见墙外有两株树，一株是枣树，还有一株也是枣树。这上面的夜的天空，奇怪而高，
我生平没有见过这样奇怪而高的天空。他仿佛要离开人间而去，使人们仰面不再看见。
"""
RU = """Глава первая
Собака бежала по улице. Собака искала хозяина. Хозяин был далеко.
Глава вторая
Вечером собака спала у двери. Ей снился хозяин и тёплый дом.
"""


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, slot, json_schema=None, **kw):
        self.calls.append(messages)
        lines = messages[1]["content"].splitlines()
        out = []
        for ln in lines:
            w = ln.split(" (example:")[0]
            # like the real model: the stress mark lands in "word" itself (бе\u0301дный)
            stressed = w[:1] + "\u0301" + w[1:] if "а" <= w[:1] <= "я" else w
            out.append({"word": stressed, "es": f"es-{w}", "en": f"en-{w}"})
        return {"words": out}


def test_levels():
    assert level_size("zh", "A1 (HSK 1)") == 1 and level_size("zh", "HSK 3") == 3 and level_size("zh", "B1") == 3
    assert level_size("ru", "A1") == 700 and level_size("en", "C2") == 25000 and level_size("ru", "") == 3000
    assert hsk_levels()["爸爸"] == 1 and len(hsk_levels()) > 10000
    zh1 = baseline("zh", 1)
    assert "我" in zh1 and "朋友" in zh1 and "枣树" not in zh1
    ru = baseline("ru", 700)
    assert len(ru) >= 700 and "быть" in ru and "дом" in ru and "в" in ru, len(ru)
    assert "хозяин" not in baseline("ru", 300)
    assert Vocab.knows("看书", zh1, {}, "zh"), "看书 = 看 + 书"
    assert not Vocab.knows("枣树", zh1, {}, "zh") and Vocab.knows("枣树", zh1, {"枣树": "known"}, "zh")
    assert not Vocab.knows("书", zh1, {"书": "learning"}, "zh"), "evidence beats the starting point"
    assert label(0.99) == "comfortable" and label(0.96) == "with effort" and label(0.9) == "hard"
    print("ok levels (HSK and frequency starting points, compounds, overrides)")


async def _flow():
    db = DB(":memory:")
    Memory(db)
    vault = Vault(TMP / "vault")
    lib = library.Library({"library": {"chunk_tokens": 200, "overlap": 20, "embed_batch": 8}}, db, FakeEmbedder(),
                          vault, FakeEngine())
    persona = Persona(CFG, db, TMP / "vault")
    db.set_setting("level_zh", "A1 (HSK 1)")
    db.set_setting("level_ru", "A1")
    v = Vocab({**CFG, "llm": {"background_slot": 1}}, db, lib, persona, FakeLLM())
    lib.note_extra = v.note_lines
    ids = {}
    for name, text in (("easy.txt", EASY_ZH), ("hard.txt", HARD_ZH), ("dog.txt", RU)):
        doc, _ = await lib.add(text.encode(), name, "web", "web")
        await asyncio.gather(*lib.tasks)
        ids[name] = doc["id"]
    await lib.run()
    easy, hard, dog = (lib.document(ids[n]) for n in ("easy.txt", "hard.txt", "dog.txt"))
    ce, ch = v.coverage(easy)["book"], v.coverage(hard)["book"]
    assert ce["coverage"] > 0.85 and ch["coverage"] < 0.6, (ce, ch)  # HSK 1 lacks 可爱, 每, 玩
    assert ch["learn_for_98"] > ce["learn_for_98"], (ce, ch)
    assert v.difficulty_line(easy).startswith(f"{int(ce['coverage'] * 100)}% known (")
    unknown = dict(v.unknown_words(dog, zipf_min=0))
    assert "хозяин" in unknown and unknown["хозяин"] == 3 and "собака" in unknown, unknown

    # their messages: uncorrected use → known; a woven word → learning, then known; a correction's word → learning
    before = v.coverage(dog)["book"]["coverage"]
    v.from_his_messages(["Собака бежит"], "ru", fixes=[])
    assert v.rows("ru")["собака"]["status"] == "known" and v.coverage(dog)["book"]["coverage"] > before
    v._upsert("ru", "хозяин", status="new", exposures=1)
    v.from_his_messages(["Где хозяин?"], "ru", fixes=[])
    assert v.rows("ru")["хозяин"]["status"] == "learning"
    v.from_his_messages(["Хозяин дома"], "ru", fixes=None)
    assert v.rows("ru")["хозяин"]["status"] == "known"
    v.from_his_messages(["Я иду в магазин вчера"], "ru", fixes=[
        {"original": "иду в магазин вчера", "better": "ходил в магазин вчера", "type": "vocabulary", "why": "past"}])
    rows = v.rows("ru")
    assert rows["ходить"]["status"] == "learning" and "идти" not in rows, "the corrected word isn't 'known'"
    v.from_his_messages(["hola amigo"], "es")
    assert not v.rows("es"), "Spanish is their own language: no vocabulary"

    # glosses: Russian with stress marks, Chinese with pinyin, each with an example sentence from the book
    db.set_setting("current_book", {"id": dog["id"], "chapter": None})
    todo = v.gloss_todo()
    assert todo[0][0]["id"] == dog["id"], "the book they're reading first"
    await v.run()
    r = v.rows("ru")
    dog_rows = [x for x in r.values() if x["example_doc"] == dog["id"]]
    assert dog_rows and all(x["gloss"] == "es-" + x["lemma"] and x["example"] for x in dog_rows), dog_rows
    assert r["собака"]["gloss"] is None, "a word they already know isn't glossed"
    hard_rows = [x for x in v.rows("zh").values() if x["example_doc"] == hard["id"]]
    assert hard_rows and all(x["reading"] and x["gloss"].startswith("es-") for x in hard_rows), hard_rows
    sky = next((x for x in hard_rows if x["lemma"] == "天空"), None)  # 枣树 is too rare (Zipf < 3) to gloss first
    assert sky and sky["reading"] == "tiān kōng" and "天空" in sky["example"], [(x["lemma"], x["reading"]) for x in hard_rows]
    assert not v.gloss_todo(), "everything glossed"
    note = vault.read(lib.document(hard["id"])["note_path"])
    assert "- Difficulty for you:" in note and "## Words to learn" in note and "天空 [tiān kōng] — es-天空 / en-天空" in note, note
    v._upsert("ru", "кот", status="new")
    v.from_his_messages(["У меня кот"], "ru", fixes=[])  # not woven yet (no exposures): they know it
    assert v.rows("ru")["кот"]["status"] == "known"
    assert all(x["reading"] and "\u0301" in x["reading"] for x in dog_rows), dog_rows  # the stress, from "word"
    print("ok vocabulary (coverage, evidence, glosses, readings, examples, notes)")

    # P4.5: while they read the Russian book and practise Russian, one turn in four gets a word
    db.set_setting("practice_lang", "ru")
    notes = [await v.before_turn([], batch_id=i) for i in range(1, 9)]
    offered = [i + 1 for i, n in enumerate(notes) if n]
    assert offered == [1, 5], notes
    assert notes[0][0].startswith("a word from «dog» they haven't learned yet, for you to use once only if it fits "
                                  "naturally: "), notes[0]
    word = v.db.get_setting("weave_offer")["lemma"]
    assert v.rows("ru")[word]["tries"] == 2, "she didn't use it the first time: offered again"
    await v.after_turn(5, [], [{"text": f"Сегодня {word}!"}], "web")
    assert v.rows("ru")[word]["exposures"] == 1 and v.db.get_setting("weave_offer") is None
    v.from_his_messages([f"{word} хорошо"], "ru", fixes=[])
    assert v.rows("ru")[word]["status"] == "learning", "they picked up a word she wove in"
    db.set_setting("practice_lang", "en")
    assert not [n for i in range(6) if (n := await v.before_turn([], batch_id=20 + i))], "not in another language"
    print("ok weaving (one in four, exposures, their use)")


def test_flow():
    asyncio.run(_flow())


if __name__ == "__main__":
    test_levels()
    test_flow()
    print("all vocab tests passed")
