"""Library ingestion (P4.1): extraction per format, words, chunking, dedupe, resuming embeddings, inbox.

Every fixture is built in code. Run inside the app image:
docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_library
"""
import asyncio
import io
import os
import pathlib
import tempfile
import time
import zipfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import extract, library, words  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_memory import fake_embed  # noqa: E402

PARA = ("The river was wide and slow that summer. We walked along it every evening after dinner. "
        "Nobody talked much, because the frogs were loud and the air smelled of rain. ")


# fixtures ----------------------------------------------------------------------------------------
def make_epub(epub3=True):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        nav = '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>' if epub3 else \
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>The Slow River</dc:title><dc:creator>Ana Writer</dc:creator><dc:language>en</dc:language></metadata>
<manifest>{nav}
<item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>
<item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="c1b" href="text/ch1b.xhtml" media-type="application/xhtml+xml"/>
<item id="c2" href="text/ch%202.xhtml" media-type="application/xhtml+xml"/>
<item id="css" href="style.css" media-type="text/css"/></manifest>
<spine{'' if epub3 else ' toc="ncx"'}><itemref idref="cover"/><itemref idref="c1"/><itemref idref="c1b"/><itemref idref="c2"/></spine>
</package>""")
        z.writestr("OEBPS/nav.xhtml", """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol><li><a href="text/ch1.xhtml">One: The River</a>
<ol><li><a href="text/ch1.xhtml#s2">A subsection</a></li></ol></li>
<li><a href="text/ch%202.xhtml#top">Two: Kanji</a></li></ol></nav></body></html>""")
        z.writestr("OEBPS/toc.ncx", """<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>
<navPoint id="p1"><navLabel><text>One: The River</text></navLabel><content src="text/ch1.xhtml"/></navPoint>
<navPoint id="p2"><navLabel><text>Two: Kanji</text></navLabel><content src="text/ch%202.xhtml"/></navPoint>
</navMap></ncx>""")
        page = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>x</title><style>p{{}}</style></head><body>{}</body></html>'
        z.writestr("OEBPS/text/cover.xhtml", page.format("<p>Cover page</p>"))
        z.writestr("OEBPS/text/ch1.xhtml", page.format(f"<h1>One</h1><p>{PARA}</p><p>Second&#160;paragraph &amp; more.</p>"))
        z.writestr("OEBPS/text/ch1b.xhtml", page.format("<p>Still chapter one.<br/>A new line.</p>"))
        z.writestr("OEBPS/text/ch 2.xhtml", page.format(
            "<h1>Two</h1><p>The sign said <ruby>漢<rp>(</rp><rt>kan</rt><rp>)</rp>字<rt>ji</rt></ruby> in red.</p>"
            "<script>var x = 'no';</script>"))
        z.writestr("OEBPS/style.css", "p { margin: 0 }")
    return b.getvalue()


def make_fb2(encoding="windows-1251"):
    body = """<?xml version="1.0" encoding="{enc}"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"><description><title-info>
<author><first-name>Антон</first-name><last-name>Чехов</last-name></author><book-title>Рассказы</book-title>
</title-info></description><body><title><p>Рассказы</p></title>
<section><title><p>Толстый и тонкий</p></title><p>На вокзале Николаевской железной дороги встретились два приятеля.</p>
<section><title><p>I</p></title><p>Один толстый, другой тонкий.</p></section></section>
<section><title><p>Хамелеон</p></title><p>Через базарную площадь идёт полицейский надзиратель Очумелов.</p><empty-line/>
<p>Кругом тишина.</p></section></body>
<body name="notes"><section><p>Примечание, которое не нужно.</p></section></body></FictionBook>"""
    return body.format(enc=encoding).encode(encoding)


def make_docx():
    import docx
    d = docx.Document()
    d.core_properties.title = "Notes on Learning"
    d.core_properties.author = "E. S."
    d.add_heading("Notes on Learning", 0)
    d.add_heading("Chapter One", 1)
    d.add_paragraph(PARA)
    d.add_heading("Details", 2)
    d.add_paragraph("A smaller heading stays inside chapter one.")
    d.add_heading("Chapter Two", 1)
    d.add_paragraph("The second chapter is short.")
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def make_pdf(pages, outline=None, title="A Small PDF"):
    """A minimal PDF by hand: Helvetica text, one content stream per page, optional outline
    [(title, page index)]. Offsets in the xref table are computed, so pdfium reads it cleanly."""
    objs = {}
    n_pages = len(pages)
    page_ids = [10 + 2 * i for i in range(n_pages)]
    objs[1] = "<< /Type /Catalog /Pages 2 0 R" + (" /Outlines 3 0 R" if outline else "") + " >>"
    objs[2] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n_pages} >>"
    objs[4] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    objs[5] = f"<< /Title ({title}) /Author (Pdf Author) >>"
    for i, lines in enumerate(pages):
        ops = "BT /F1 11 Tf 14 TL 72 760 Td " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
        objs[page_ids[i]] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page_ids[i] + 1} 0 R "
                             "/Resources << /Font << /F1 4 0 R >> >> >>")
        objs[page_ids[i] + 1] = f"<< /Length {len(ops)} >>\nstream\n{ops}\nendstream"
    if outline:
        ids = [100 + i for i in range(len(outline))]
        objs[3] = f"<< /Type /Outlines /First {ids[0]} 0 R /Last {ids[-1]} 0 R /Count {len(ids)} >>"
        for k, (t, page) in enumerate(outline):
            links = (f" /Prev {ids[k - 1]} 0 R" if k else "") + (f" /Next {ids[k + 1]} 0 R" if k + 1 < len(ids) else "")
            objs[ids[k]] = f"<< /Title ({t}) /Parent 3 0 R /Dest [{page_ids[page]} 0 R /Fit]{links} >>"
    out, offsets = b"%PDF-1.4\n", {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{objs[num]}\nendobj\n".encode("latin-1")
    xref = len(out)
    size = max(objs) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for num in range(1, size):
        out += (f"{offsets[num]:010d} 00000 n \n" if num in offsets else "0000000000 65535 f \n").encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R /Info 5 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


PDF_LINES = ["The first line of a paragraph that wraps across sev-", "eral lines because it is quite long and",
             "keeps going until it ends here.", "", "A second paragraph starts after a blank line and it",
             "continues for a while longer than the first one."]


# tests -------------------------------------------------------------------------------------------
def test_words():
    t = words.tokens("Вчера я видел собак. 我喜欢北京的猫。 The dogs were running", "en")
    lemmas = {(lemma, kind) for _, lemma, kind, _ in t}
    assert ("собака", words.WORD) in lemmas and ("видеть", words.WORD) in lemmas, lemmas
    assert ("北京", words.NAME) in lemmas and ("喜欢", words.WORD) in lemmas, lemmas
    assert ("dog", words.WORD) in lemmas and ("run", words.WORD) in lemmas, lemmas
    assert "собака" in words.search_text("Собаки лают") and "ежик" in words.search_text("Ёжик")
    assert words.query_terms("what is the book about dogs?") == ["book", "dog"], words.query_terms("what is the book about dogs?")
    print("ok words")


def test_epub():
    for epub3 in (True, False):
        doc = extract.extract(make_epub(epub3), "river.epub")
        assert (doc.title, doc.author, doc.lang) == ("The Slow River", "Ana Writer", "en"), doc
        titles = [t for t, _ in doc.sections]
        assert titles == [None, "One: The River", "Two: Kanji"], titles
        one, two = doc.sections[1][1], doc.sections[2][1]
        assert "Still chapter one.\nA new line." in one and "Second paragraph & more." in one, one
        assert "漢字 in red" in two and "kan" not in two and "var x" not in two, two
    print("ok epub (nav and ncx, joined spine items, ruby dropped)")


def test_fb2():
    for data, name in ((make_fb2(), "chekhov.fb2"), (make_fb2("utf-8"), "chekhov.fb2")):
        doc = extract.extract(data, name)
        assert (doc.title, doc.author, doc.lang) == ("Рассказы", "Антон Чехов", "ru"), (doc.title, doc.author)
        assert [t for t, _ in doc.sections] == ["Толстый и тонкий", "Хамелеон"], doc.sections
        assert "I\nОдин толстый" in doc.sections[0][1] and "Примечание" not in str(doc.sections)
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("book/chekhov.fb2", make_fb2())
    assert len(extract.extract(b.getvalue(), "chekhov.fb2.zip").sections) == 2
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("readme.txt", "hi")
    try:
        extract.extract(b.getvalue(), "other.zip")
        raise AssertionError("a zip without fb2 should be refused")
    except extract.Unreadable:
        pass
    print("ok fb2 (cp1251, utf-8, zipped, notes body skipped)")


def test_docx():
    doc = extract.extract(make_docx(), "notes.docx")
    assert (doc.title, doc.author) == ("Notes on Learning", "E. S."), doc
    assert [t for t, _ in doc.sections] == ["Chapter One", "Chapter Two"], doc.sections
    assert "Details\nA smaller heading" in doc.sections[0][1]
    print("ok docx")


def test_pdf():
    pages = [["Page one opens."] + PDF_LINES, ["Page two opens."] + PDF_LINES,
             ["Chapter two begins on this page with enough words to count as text."] * 3, ["Page four."] + PDF_LINES]
    doc = extract.extract(make_pdf(pages, outline=[("Opening", 0), ("Second", 2)]), "small.pdf")
    assert (doc.title, doc.author) == ("A Small PDF", "Pdf Author"), doc
    assert [t for t, _ in doc.sections] == ["Opening", "Second"], [t for t, _ in doc.sections]
    first = doc.sections[0][1]
    assert "across several lines because" in first, first
    assert "ends here.\n" in first and "The first line" in first, first
    doc = extract.extract(make_pdf([[f"Page {i} opens with its own line."] + PDF_LINES for i in range(12)]), "plain.pdf")
    assert [t for t, _ in doc.sections] == ["pages 1–10", "pages 11–12"], [t for t, _ in doc.sections]
    try:
        extract.extract(make_pdf([[]] * 3), "scan.pdf")
        raise AssertionError("a scan must ask for OCR (SB3)")
    except extract.NeedsOCR as e:
        assert e.pages == [0, 1, 2] and e.total == 3
    try:
        extract.extract(make_pdf([[]] * 3), "scan.pdf", ocr=lambda i: "")
        raise AssertionError("OCR that finds nothing: refused")
    except extract.Unreadable as e:
        assert "even with OCR" in str(e)
    print("ok pdf (outline, page sections, de-hyphenation, scanned)")


def test_txt_md_html():
    trad = "第一章 開始\n\n這是一個關於學習語言的故事。我們每天說話，學習新的詞語。\n\n第二章 結束\n\n後來他們都會說中文了。" * 1
    doc = extract.extract((trad + "\n\n" + "他們說話很慢。" * 40).encode("gb18030"), "故事.txt")
    assert doc.lang == "zh" and doc.meta.get("traditional"), (doc.lang, doc.meta)
    assert [t for t, _ in doc.sections] == ["第一章 开始", "第二章 结束"], doc.sections
    assert "这是一个关于学习语言的故事" in doc.sections[0][1], doc.sections[0][1]
    md = "# My Notes\n\nIntro line.\n\n## First\n\n" + PARA + "\n\n## Second\n\nMore text here.\n"
    doc = extract.extract(md.encode(), "notes.md")
    assert doc.title == "My Notes" and [t for t, _ in doc.sections] == [None, "First", "Second"], doc.sections
    txt = "\n\n".join(["CHAPTER I.", PARA, "CHAPTER II. The Return", PARA, "Part of the reason was the rain.",
                       "Mix.", "III", PARA])
    doc = extract.extract(txt.encode(), "gutenberg.txt")
    assert [t for t, _ in doc.sections] == ["CHAPTER I.", "CHAPTER II. The Return", "III"], doc.sections
    assert doc.title == "gutenberg"
    html = f"<html><head><title>Web Article</title></head><body><h2>A</h2><p>{PARA}</p><h2>B</h2><p>x y z</p></body></html>"
    doc = extract.extract(html.encode(), "page.html")
    assert doc.title == "Web Article" and [t for t, _ in doc.sections] == ["A", "B"], doc.sections
    long_text = "\n\n".join([PARA * 3] * 60)
    doc = extract.extract(long_text.encode(), "long.txt")
    assert len(doc.sections) > 2 and doc.sections[0][0] == "part 1", [t for t, _ in doc.sections]
    pg = ("The Project Gutenberg eBook of Things\nThis ebook is for the use of anyone anywhere.\n"
          "*** START OF THE PROJECT GUTENBERG EBOOK THINGS ***\n\n" + PARA + "\n\n*** END OF THE PROJECT GUTENBERG EBOOK "
          "THINGS ***\n\nSection 1. General Terms of Use")
    doc = extract.extract(pg.encode(), "pg.txt")
    assert doc.sections == [(None, PARA.strip())], doc.sections
    html = (f"<html><head><title>{'A Modest Proposal / For preventing the children of poor people from being a burden'}"
            f"</title></head><body><h1>S会馆里有三间屋，相传是往昔曾在院子里的槐树上缢死过一个女人的，现在</h1>"
            f"<p>{'这是一个很长的故事。' * 3000}</p><h1>THE FULL PROJECT GUTENBERG™ LICENSE</h1><p>terms</p></body></html>")
    doc = extract.extract(html.encode(), "x.html")
    assert doc.title == "A Modest Proposal" and doc.meta["full_title"].endswith("burden"), (doc.title, doc.meta)
    titles = [t for t, _ in doc.sections]
    assert len(titles) == 5 and titles[0].endswith("(part 1)") and len(titles[0]) < 60 and "LICENSE" not in str(titles), titles
    try:
        extract.extract(b"x", "book.mobi")
        raise AssertionError("mobi is not supported")
    except extract.Unreadable:
        pass
    print("ok txt/md/html (gb18030 + traditional, chapter lines, md title, parts)")


def test_nested_headings():
    """A collection: two stories (h1), the long one split by its own chapters (h2), the short one kept whole."""
    long_ch = "<p>" + ("Собака бежала по улице и искала хозяина. " * 500) + "</p>"
    page = ("<html><body><h1>Смерть чиновника</h1><p>Короткий рассказ.</p><h2>Note</h2><p>x</p>"
            f"<h1>Каштанка</h1><h2>Глава первая</h2>{long_ch}<h2>Глава вторая</h2>{long_ch}</body></html>")
    doc = extract.extract(page.encode(), "stories.html")
    assert [t for t, _ in doc.sections] == ["Смерть чиновника", "Каштанка — Глава первая", "Каштанка — Глава вторая"], \
        [t for t, _ in doc.sections]
    assert "Note" in doc.sections[0][1], "a short section keeps its sub-headings as lines"
    print("ok nested headings")


def test_chunks():
    units = [(f"s{i}", 50, f"s{i}") for i in range(20)]
    groups = library.chunk(units, 600, 80)
    assert all(sum(u[1] for u in g) <= 600 for g in groups), [len(g) for g in groups]
    assert groups[0][-1] == groups[1][0], "the next chunk starts with the previous last sentence (overlap)"
    assert groups[-1][-1] == units[-1] and [len(g) for g in groups] == [12, 9], [len(g) for g in groups]
    assert [len(g) for g in library.chunk([(f"s{i}", 100, "") for i in range(13)], 600, 80)] == [6, 6, 1]
    assert library.split_long("word " * 2000, 600)[0].endswith("word")
    assert all(extract.estimate_tokens(p) <= 601 for p in library.split_long("字" * 1500, 600))
    doc = extract.Doc(title="T", lang="en", sections=[("One", (PARA + "\n") * 40)])
    (title, chunks, counts), = library.prepare(doc, 600, 80)
    assert len(chunks) > 3 and all(t <= 600 for _, t, *_ in chunks), [t for _, t, *_ in chunks]
    assert counts[("en", "river")] == 40 and counts[("en", "frog")] == 40, counts.most_common(5)
    assert "frog" in chunks[0][2]
    ru = extract.Doc(title="Т", lang="ru", sections=[(None, "Собаки лают. Москва большая.")])
    (_, _, c), = library.prepare(ru, 600, 80)
    assert c[("ru", "собака")] == 1 and not any(k[1] == "москва" for k in c), c
    print("ok chunks (size, overlap, word counts without names)")


class FakeEngine:
    def __init__(self):
        self.events = []

    async def publish(self, ev):
        self.events.append(ev)

    def said(self):
        return [e["message"]["text"] for e in self.events if e["type"] == "message"]


class FakeEmbedder:
    def __init__(self, fail_after=None):
        self.calls, self.fail_after = 0, fail_after

    async def embed(self, texts, query=False):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise asyncio.CancelledError()
        return [fake_embed(t) for t in texts]


async def _library_flow():
    db = DB(":memory:")
    Memory(db)
    vault = Vault(TMP / "vault")
    engine = FakeEngine()
    lib = library.Library({"library": {"chunk_tokens": 200, "overlap": 40, "embed_batch": 2}}, db, FakeEmbedder(1),
                          vault, engine)
    doc, reply = await lib.add(make_epub(), "river.epub", "web", "web")
    assert reply is None and doc["status"] == "queued"
    await asyncio.gather(*lib.tasks)
    doc = lib.document(doc["id"])
    assert doc["status"] == "indexing" and doc["title"] == "The Slow River", doc
    assert "📚 Reading «The Slow River»… (3 chapters)" in engine.said(), engine.said()
    n_chunks = len(lib.chunks(doc["id"]))
    hit = db.conn.execute("SELECT rowid FROM lib.chunks_fts WHERE chunks_fts MATCH 'frog'").fetchall()
    assert hit, "chunks are keyword-searchable before they're embedded"
    assert lib.pending()
    try:
        await lib.run()  # the first batch embeds, the second is "cancelled" (they came back)
        raise AssertionError("expected the cancel")
    except asyncio.CancelledError:
        pass
    done = db.conn.execute("SELECT COUNT(*) FROM lib.chunks WHERE embedded = 1").fetchone()[0]
    assert done == min(2, n_chunks) and lib.document(doc["id"])["status"] == "indexing", done
    lib.embedder = FakeEmbedder()
    await lib.run()
    doc = lib.document(doc["id"])
    assert doc["status"] == "ready" and not lib.pending(), doc
    assert db.conn.execute("SELECT COUNT(*) FROM lib.chunks_vec").fetchone()[0] == n_chunks
    assert "📚 «The Slow River» is in the library (3 chapters)" in engine.said()
    note = vault.read(doc["note_path"])
    assert doc["note_path"] == "Library/The Slow River.md" and "Ana Writer" in note and "2. One: The River" in note, note
    _, reply = await lib.add(make_epub(), "copy.epub", "web", "web")
    assert reply == "📚 «The Slow River» is already in the library", reply
    _, reply = await lib.add(b"x", "book.mobi", "web", "web")
    assert reply.startswith("📚 I can't read"), reply
    # a scanned PDF waits for OCR (SB3, in quiet time) and says so; an unreadable file fails and can be sent again
    doc2, _ = await lib.add(make_pdf([[]] * 4), "scan.pdf", "telegram", "telegram")
    await asyncio.gather(*lib.tasks)
    assert lib.document(doc2["id"])["status"] == "ocr_pending" and lib.ocr_pending(), lib.document(doc2["id"])
    assert any("OCR" in t for t in engine.said())
    db.conn.execute("UPDATE lib.documents SET status = 'failed: test' WHERE id = ?", (doc2["id"],))
    doc3, reply = await lib.add(make_pdf([[]] * 4), "scan.pdf", "telegram", "telegram")
    assert reply is None and doc3["id"] != doc2["id"] and lib.document(doc2["id"]) is None
    await asyncio.gather(*lib.tasks)
    db.conn.execute("UPDATE lib.documents SET status = 'failed: test' WHERE id = ?", (doc3["id"],))
    # a long paste: the message keeps the start, the text goes into the library quietly
    long_text = "Notes from the meeting\n" + "\n".join([PARA] * 100)
    short = await lib.shorten_paste("web", long_text)
    assert short.startswith("Notes from the meeting") and "📄 (pasted text, about" in short and \
        "saved in the library as «Notes from the meeting" in short and len(short) < 1700, short
    assert await lib.shorten_paste("web", "short message") == "short message"
    await asyncio.gather(*lib.tasks)
    paste = lib.documents()[-1]
    assert paste["source"] == "paste" and paste["status"] == "indexing" and paste["kind"] == "txt", paste
    assert not any(t.startswith("📚 Reading «Notes") for t in engine.said()), "pastes don't announce reading"
    # restart while queued: resume() extracts again from the original
    doc4, _ = await lib.add(make_docx(), "notes.docx", "web", "web")
    for t in list(lib.tasks):
        t.cancel()
    await asyncio.gather(*lib.tasks, return_exceptions=True)
    assert lib.document(doc4["id"])["status"] == "queued"
    await lib.resume()
    await asyncio.gather(*lib.tasks)
    assert lib.document(doc4["id"])["status"] == "indexing"
    # the inbox: settled files are taken in, unreadable ones are parked, fresh ones wait
    inbox = vault.path(library.INBOX)
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "chekhov.fb2").write_bytes(make_fb2())
    (inbox / "book.mobi").write_bytes(b"x")
    (inbox / "fresh.txt").write_text("still syncing")
    (inbox / ".hidden.txt").write_text("hidden")
    old = time.time() - 60
    for name in ("chekhov.fb2", "book.mobi", ".hidden.txt"):
        os.utime(inbox / name, (old, old))
    await lib.scan_inbox()
    await asyncio.gather(*lib.tasks)
    assert sorted(p.name for p in inbox.iterdir()) == [".hidden.txt", "_failed", "fresh.txt"], list(inbox.iterdir())
    assert (inbox / "_failed" / "book.mobi").exists()
    fb2 = next(d for d in lib.documents() if d["source"] == "inbox")
    assert fb2["title"] == "Рассказы" and fb2["lang"] == "ru" and fb2["status"] == "indexing", fb2
    assert db.conn.execute("SELECT count FROM lib.doc_words WHERE doc_id = ? AND lemma = 'толстый'",
                           (fb2["id"],)).fetchone()[0] == 2
    await lib.run()
    assert all(d["status"] == "ready" or d["status"].startswith("failed") for d in lib.documents()), lib.documents()
    print("ok library (dedupe, cancelled embedding resumed, note, failed retry, paste, restart, inbox)")


def test_library():
    asyncio.run(_library_flow())


if __name__ == "__main__":
    for t in (test_words, test_epub, test_fb2, test_docx, test_pdf, test_txt_md_html, test_nested_headings, test_chunks, test_library):
        t()
    print("all library tests passed")
