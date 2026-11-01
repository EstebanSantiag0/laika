"""Library evaluation (PLAN P4.2 calibration, P4.7): public-domain test books in a throwaway library.

Books (not in git; fetch them once into a folder of their own). Since 2026-10-25 a copy is kept in
data/eval/books/, with the library from the last `ingest` in data/eval/library/ (pass that as <eval dir>
to skip the ~20 min ingest):
  EN  Project Gutenberg 11 (Alice's Adventures in Wonderland) and 1080 (A Modest Proposal), EPUB
  ZH  Project Gutenberg 27166 (Lu Xun, 呐喊; traditional characters), EPUB
  RU  Chekhov, «Смерть чиновника» and «Каштанка» from ru.wikisource.org, joined into chekhov.html

  ingest   add every book in /books to the library in /eval (LAIKA_DATA) and embed it with the live
           `embed` service; prints chapters, timings and embed speed. Run once (~20 min).
  llm      summaries (P4.3) and glosses (P4.4) with the live model for one book (default «Рассказы»), on
           the background slot; then coverage of every book at the levels given (P4.4).
  eval     retrieval: for each question, where the right chapter lands (recall@2) and how the best
           match scores (cosine, z within the book); for unrelated messages, how high the best match
           gets while each book is "the book he's reading" (the bar must stay above these).
           Also the hook's latency per turn.

Run (host, from ~/laika):
  docker run --rm --network laika_default -v "$PWD/app:/app" -v "$PWD/scripts:/scripts:ro" \
    -v <books dir>:/books:ro -v <eval dir>:/eval -e LAIKA_DATA=/eval -e LAIKA_VAULT=/eval/vault \
    -e PYTHONPATH=/app -w /app local/laika-app:0.1 python /scripts/library_eval.py ingest|eval
"""
import asyncio
import pathlib
import statistics
import sys
import time

from laika import books as books_mod
from laika.books import Books
from laika.db import DB
from laika.embed import Embedder
from laika.library import Library
from laika.memory import Memory
from laika.persona import Persona

CFG = {"embed": {"url": "http://embed:8080"}, "memory": {"tz": "Europe/Madrid"},
       "language": {"default": "en"}, "library": {"chunk_tokens": 600, "overlap": 80, "embed_batch": 4}}

# (question, book title fragment, where the answer is): a section title fragment, or "text:<word>" for a chunk
# containing that word (Gutenberg's 呐喊 has no usable chapters: one TOC entry, paragraphs tagged as headings).
QUESTIONS = [
    ("Why did Alice follow the White Rabbit down the hole?", "Alice", "Rabbit-Hole"),
    ("What happened at the Mad Hatter's tea party?", "Alice", "Tea-Party"),
    ("What did the Queen keep shouting at everyone?", "Alice", "Croquet"),
    ("What did the Caterpillar ask Alice?", "Alice", "Caterpillar"),
    ("How did Alice end up swimming in her own tears?", "Alice", "Pool of Tears"),
    ("Who stole the tarts?", "Alice", "Tarts"),
    ("What does Swift propose to do with the children of the poor?", "Modest", ""),
    ("Почему Червяков так боялся генерала?", "Рассказы", "Смерть чиновника"),
    ("Как Каштанка потерялась?", "Рассказы", "Дурное поведение"),
    ("Что случилось с гусем Иваном Ивановичем ночью?", "Рассказы", "Беспокойная ночь"),
    ("What happened at Kashtanka's first circus show?", "Рассказы", "Неудачный дебют"),
    ("狂人为什么觉得别人要吃他？", "呐喊", "text:吃人"),
    ("孔乙己为什么总是被人笑？", "呐喊", "text:孔乙己"),
    ("What did the parents buy to cure their sick son in Lu Xun's story about medicine?", "呐喊", "text:馒头"),
    ("闰土小时候是什么样的？", "呐喊", "text:闰土"),
]
UNRELATED = ["I made pasta for dinner tonight", "my boss yelled at me in front of everyone today",
             "I'm so tired, I barely slept", "what kind of music do you like?", "do you think free will exists?",
             "my cousin called me, she's moving to Monterrey", "the weather is really nice today",
             "Я сегодня ходил в магазин и купил хлеб", "Мне нравится учить русский язык", "我今天很累，想早点睡觉",
             "你喜欢喝咖啡还是茶？", "hoy fui al gimnasio y luego vi una película",
             "I want to learn more Chinese characters", "what should I cook tomorrow?"]


def open_library():
    db = DB(pathlib.Path("/eval/laika.db"))
    Memory(db)

    class Quiet:
        async def publish(self, event):
            if event.get("type") == "message":
                print("   ", event["message"]["text"])
    embedder = Embedder(CFG)
    lib = Library(CFG, db, embedder, _NoVault(), Quiet())
    return db, lib, embedder


class _NoVault:
    root = pathlib.Path("/eval/vault")

    def write(self, *a, **k):
        return True

    def path(self, rel):
        return self.root / rel

    def section(self, rel):
        return None


async def ingest():
    db, lib, _ = open_library()
    for path in sorted(pathlib.Path("/books").iterdir()):
        if path.suffix not in (".epub", ".html", ".fb2", ".txt", ".pdf"):
            continue
        t0 = time.time()
        doc, reply = await lib.add(path.read_bytes(), path.name, "eval", "web")
        await asyncio.gather(*lib.tasks)
        doc = lib.document(doc["id"])
        print(f"{path.name}: «{doc['title']}» {doc['lang']} {doc['tokens']} tokens, extracted in {time.time() - t0:.1f} s")
        for s in lib.sections(doc["id"]):
            print(f"   {s['ord'] + 1}. {s['title']}")
    t0 = time.time()
    tokens = sum(d["tokens"] for d in lib.documents(("indexing",)))
    await lib.run()
    dt = time.time() - t0
    print(f"embedded {tokens} tokens in {dt:.0f} s = {tokens / max(dt, 1):.0f} tok/s")


def right_chunk(chunk, where):
    if where.startswith("text:"):
        return where[5:] in chunk["text"]
    return where in (chunk["s_title"] or "")


def doc_by(lib, fragment):
    return next(d for d in lib.documents() if fragment in d["title"])


async def evaluate():
    db, lib, embedder = open_library()
    persona = Persona(CFG, db, pathlib.Path("/eval/vault"))
    b = Books(CFG, db, lib, embedder, persona)
    docs = lib.searchable()
    print(f"{len(docs)} books: " + ", ".join(f"«{d['title']}»" for d in docs))
    hits, rows = 0, []
    for q, book, section in QUESTIONS:
        doc = doc_by(lib, book)
        query = await embedder.one(q, query=True)
        code = "zh" if any("一" <= ch <= "鿿" for ch in q) else "ru" if any("а" <= ch <= "я" for ch in q.lower()) \
            else "en"
        db.conn.execute("DELETE FROM injections")
        top = b.pick(q, query, code, [doc], "named")
        secs = [p["chunk"]["s_title"] or "" for p in top]
        ok = any(right_chunk(p["chunk"], section) for p in top)
        hits += ok
        cos = lib.similarities(query, [doc["id"]])
        vals = list(cos.values())
        mean, sd = statistics.mean(vals), statistics.pstdev(vals) or 1
        right = [c for cid, c in cos.items() if right_chunk(lib.chunk_row(cid), section)]
        best_right = max(right) if right else 0
        rows.append((q, ok, max(vals), (max(vals) - mean) / sd, best_right, (best_right - mean) / sd))
        print(f"{'✓' if ok else '✗'} {q[:60]:60} → {', '.join(s[:24] for s in secs) or '(nothing)'}")
    print(f"\nrecall@2 (named book): {hits}/{len(QUESTIONS)}")
    print("best match per question: cos / z;  best chunk of the right chapter: cos / z")
    for q, ok, c, z, rc, rz in rows:
        print(f"  {c:.3f} / {z:4.1f}   {rc:.3f} / {rz:4.1f}   {q[:50]}")
    print("\nunrelated messages, with each book as the one he's reading: best cos / z")
    worst = []
    for text in UNRELATED:
        query = await embedder.one(text, query=True)
        line = []
        for doc in docs:
            cos = lib.similarities(query, [doc["id"]])
            vals = list(cos.values())
            mean, sd = statistics.mean(vals), statistics.pstdev(vals) or 1
            c = max(vals)
            line.append(f"{c:.2f}/{(c - mean) / sd:3.1f}")
            worst.append((c, (c - mean) / sd, text, doc["title"]))
        print(f"  {text[:44]:44} " + "  ".join(line))
    worst.sort(reverse=True)
    print("highest unrelated cosines:", [(round(c, 3), round(z, 1), t[:30], d[:12]) for c, z, t, d in worst[:5]])
    print("highest unrelated z:", [(round(c, 3), round(z, 1), t[:30], d[:12])
                                   for c, z, t, d in sorted(worst, key=lambda w: w[1], reverse=True)[:5]])
    passing = [(q, c, z) for q, ok, c, z, *_ in rows if c >= books_mod.COS_READING and z >= books_mod.Z_READING]
    leaks = [w for w in worst if w[0] >= books_mod.COS_READING and w[1] >= books_mod.Z_READING]
    print(f"\nwith COS_READING {books_mod.COS_READING} / Z_READING {books_mod.Z_READING}: "
          f"{len(passing)}/{len(rows)} questions would get a passage without naming the book; "
          f"{len(leaks)} unrelated leaks {[(w[2][:25], w[3][:10]) for w in leaks]}")
    # latency of the whole hook, as in a live turn (the book he's reading is Alice)
    db.set_setting("current_book", {"id": doc_by(lib, "Alice")["id"], "chapter": None})
    times = []
    for q, *_ in QUESTIONS[:6] + [(u,) for u in UNRELATED[:6]]:
        db.conn.execute("DELETE FROM injections")
        embedder.recent.clear()
        t0 = time.perf_counter()
        await embedder.one(q, query=True)  # recall embeds first in a live turn
        t1 = time.perf_counter()
        await b.before_turn([db.add_message("user", "web", q)])
        times.append((time.perf_counter() - t1) * 1000)
    print(f"hook time after the shared query embedding: median {statistics.median(times):.0f} ms, "
          f"max {max(times):.0f} ms")


async def with_model():
    from laika.llm import LLM
    from laika.summaries import Summaries
    from laika.vocab import Vocab
    db, lib, embedder = open_library()
    cfg = {**CFG, "llm": {"url": "http://llm:8080", "background_slot": 1, "max_tokens": 400,
                          "chat_template_kwargs": {"enable_thinking": False}}}
    llm = LLM(cfg)
    for code, level in (("en", "C2"), ("ru", "A1"), ("zh", "A1 (HSK 1)")):
        db.set_setting(f"level_{code}", level)
    persona = Persona(CFG, db, pathlib.Path("/eval/vault"))
    vocab = Vocab(cfg, db, lib, persona, llm)
    for d in lib.documents(("ready",)):
        line = vocab.difficulty_line(d) or "(no vocabulary: not a language he learns)"
        print(f"«{d['title']}» ({d['lang']}): {line}")
    doc = doc_by(lib, sys.argv[2] if len(sys.argv) > 2 else "Рассказы")
    db.set_setting("current_book", {"id": doc["id"], "chapter": None})
    t0 = time.time()
    s = Summaries(cfg, db, llm, lib, embedder)
    s.todo = lambda: [lib.document(doc["id"])] if [x for x in Summaries.todo(s) if x["id"] == doc["id"]] else []
    await s.run()
    print(f"\nsummaries of «{doc['title']}» in {time.time() - t0:.0f} s")
    print("BOOK:", lib.document(doc["id"])["summary"])
    for sec in lib.sections(doc["id"])[:3]:
        print(f"ch. {sec['ord'] + 1} {sec['title']}: {sec['summary']}")
    t0 = time.time()
    vocab.gloss_todo = lambda: [(d, w) for d, w in Vocab.gloss_todo(vocab) if d["id"] == doc["id"]][:1]
    await vocab.run()
    print(f"\nglosses in {time.time() - t0:.0f} s")
    rows = [r for r in vocab.rows(doc["lang"]).values() if r["example_doc"] == doc["id"]]
    for r in rows[:20]:
        print(f"  {r['lemma']} [{r['reading'] or ''}] — {r['gloss']} / {r['gloss_en']}  · {(r['example'] or '')[:70]}")
    print(f"{len(rows)} glossed, {sum(1 for r in rows if r['reading'])} with a reading")
    await llm.client.aclose()


if __name__ == "__main__":
    asyncio.run({"ingest": ingest, "eval": evaluate, "llm": with_model}[sys.argv[1]]())
