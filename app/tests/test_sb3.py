"""SB3 (docs/PLAN-second-brain.md): PDF pages, clean text, scans.

- Chunks know their pages (page_from/page_to), the label in front of each embedded chunk names them,
  her book citations name them, and search results return them.
- Running headers/footers and page numbers repeated on most pages are gone; a contents page and a
  copyright page are skipped; the document's meta counts pages (text / ocr / empty / skipped).
- A PDF with scanned pages waits as `ocr_pending`; the OCR job reads the pages (kept per page, so an
  interruption loses nothing), then the document is extracted with that text and the pages counted.
- Real Tesseract reads a rendered page.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_sb3
"""
import asyncio
import os
import pathlib
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

from laika import books, extract, library  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_library import PARA, FakeEmbedder, FakeEngine, make_pdf  # noqa: E402

LINES = [s.strip() + "." for s in PARA.split(".") if s.strip()]


def page(n, body):
    return ["The Slow River - A Novel", *body, f"Page {n} of 8"]


def book():
    pages = [["Contents"] + [f"Chapter {i} ........ {i * 3}" for i in range(1, 8)],
             ["Copyright 2026 Ana Writer. All rights reserved.", "ISBN 978-0-00-000000-0"]]
    for n in range(3, 9):
        pages.append(page(n, [f"On page {n} the heron waited by the water."] + LINES * 3))
    return make_pdf(pages, title="The Slow River")


async def main():
    doc = extract.extract(book(), "river.pdf")
    text = "\n".join(t for _, t in doc.sections)
    assert "The Slow River - A Novel" not in text and "Page 4 of 8" not in text, "running header and footer gone"
    assert "........" not in text and "ISBN" not in text, "contents and copyright pages skipped"
    assert doc.meta["pages"] == {"total": 8, "text": 8, "ocr": 0, "empty": 0, "skipped": 2}, doc.meta["pages"]

    db = DB(":memory:")
    Memory(db)
    lib = library.Library({"library": {"chunk_tokens": 60, "overlap": 0, "embed_batch": 8}}, db, FakeEmbedder(),
                          Vault(TMP / "vault"), FakeEngine())
    d, _ = await lib.add(book(), "river.pdf", "web", "web")
    await asyncio.gather(*lib.tasks)
    chunks = lib.chunks(d["id"])
    heron = [c for c in chunks if "On page 5 the heron" in c["text"]][0]
    assert heron["page_from"] <= 5 <= heron["page_to"] and heron["page_from"] >= 4, heron
    assert all(c["page_from"] and c["page_to"] >= c["page_from"] for c in chunks if c["kind"] == "text")
    assert lib.counts(d["id"])["pages"]["total"] == 8
    label = library.chapter_label(lib.document(d["id"]), 0, "pages 1–8", 5, 6)
    assert label.endswith("— p. 5–6"), label
    row = lib.chunk_row(heron["id"])
    cite = books.citation(lib.document(d["id"]), row, "the heron waited")
    assert f"p. {heron['page_from']}" in cite, cite

    # a scan: waits for OCR, then read with it
    scan = make_pdf([["x"]] * 5, title="Scan")
    s, _ = await lib.add(scan, "scan.pdf", "web", "web")
    await asyncio.gather(*lib.tasks)
    assert lib.document(s["id"])["status"] == "ocr_pending" and lib.ocr_pending()
    assert any("scanned page" in t and "OCR" in t for t in lib.engine.said())
    calls = []

    def fake_ocr(path, index, langs=library.OCR_ALL):
        calls.append(index)
        if index == 2 and len(calls) == 3:
            raise asyncio.CancelledError()  # he came back in the middle
        return f"Scanned page {index + 1} says the river is cold in winter. " * 4

    library.ocr_page = fake_ocr
    try:
        await lib.run_ocr()
    except asyncio.CancelledError:
        pass
    assert lib.document(s["id"])["status"] == "ocr_pending" and calls == [0, 1, 2]
    await lib.run_ocr()
    assert calls == [0, 1, 2, 2, 3, 4], "done pages are kept: the rest only"
    await asyncio.gather(*lib.tasks)
    doc_s = lib.document(s["id"])
    assert doc_s["status"] == "indexing" and lib.counts(s["id"])["pages"]["ocr"] == 5, lib.counts(s["id"])
    assert any("Scanned page 3" in c["text"] and c["page_from"] <= 3 <= c["page_to"] for c in lib.chunks(s["id"]))

    # real Tesseract on a rendered page
    path = TMP / "real.pdf"
    path.write_bytes(make_pdf([["The heron waited by the cold river every morning."] * 3]))
    text = await asyncio.to_thread(REAL_OCR, path, 0, "eng")
    assert "heron" in text.lower() and "river" in text.lower(), text
    print("ok sb3 (pages, labels, citations, running headers, contents/copyright, scan → OCR, resume, tesseract)")


REAL_OCR = library.ocr_page


def test_sb3():
    asyncio.run(main())


if __name__ == "__main__":
    test_sb3()
