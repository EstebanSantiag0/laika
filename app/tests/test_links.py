"""SB2 + SB4: the page or the video behind a saved link, with a fake web and a fake voice service.

- A saved article link: the page is fetched, its main text (not the menu) becomes a library document
  attached to the same item; the HTML snapshot is kept; the item gets the page's title; no new item.
- A login wall or a near-empty page: the item keeps the link, a 📚 "couldn't" notice is sent.
- A link to a PDF: added as the PDF.
- A reel: queued for the job; yt-dlp (faked) gives audio and a caption, ffmpeg converts, the voice
  service (faked) transcribes; caption + transcript become the item's document. A login wall gives up
  at once with a notice.

Run: docker run --rm -v "$PWD/app:/app" -w /app local/laika-app:0.1 python -m tests.test_links
"""
import asyncio
import os
import pathlib
import subprocess
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp())
os.environ["LAIKA_DATA"] = str(TMP / "data")
os.environ["LAIKA_VAULT"] = str(TMP / "vault")

import httpx  # noqa: E402

from laika import library, links  # noqa: E402
from laika.areas import Areas  # noqa: E402
from laika.brain import Brain  # noqa: E402
from laika.db import DB  # noqa: E402
from laika.memory import Memory  # noqa: E402
from laika.vault import Vault  # noqa: E402
from tests.test_brain import CFG, FakeLLM  # noqa: E402
from tests.test_consolidate import FakeEmbedder  # noqa: E402
from tests.test_library import PARA, FakeEngine, make_pdf  # noqa: E402

CFG = {**CFG, "voice": {"url": "http://voice:8000"}}
ARTICLE = f"""<html><head><title>Cómo regar con un Arduino | Blog de Ana</title></head><body>
<nav><a href="/">Inicio</a> <a href="/x">Suscríbete al boletín</a></nav>
<article><h1>Cómo regar con un Arduino</h1>
<p>{PARA}</p><p>Un sensor de humedad capacitivo dura más que uno resistivo. {PARA}</p><p>{PARA}</p></article>
<footer>© 2026 Ana. Todos los derechos reservados. Política de cookies.</footer></body></html>"""


def web(request):
    url = str(request.url)
    if url.endswith("/article"):
        return httpx.Response(200, html=ARTICLE)
    if url.endswith("/private"):
        return httpx.Response(403, text="login")
    if url.endswith("/empty"):
        return httpx.Response(200, html="<html><body><div id=app></div><script>x()</script></body></html>")
    if url.endswith("/paper.pdf"):
        lines = [s.strip() + "." for s in PARA.split(".") if s.strip()] * 4
        return httpx.Response(200, content=make_pdf([lines, lines]), headers={"content-type": "application/pdf"})
    if url.endswith("/v1/audio/transcriptions"):
        return httpx.Response(200, json={"text": "Hoy les enseño a hacer ramen en casa.", "language": "es"})
    return httpx.Response(404)


def fake_download(url, folder):
    if "private" in url:
        raise links.Unreadable("it needs a login")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(folder / "media.m4a")], check=True)
    return {"title": "Video by chef", "description": "Ramen casero 🍜\nReceta completa", "uploader": "chef",
            "duration": 65}


async def main():
    db = DB(":memory:")
    mem = Memory(db)
    areas = Areas(CFG, db)
    vault = Vault(TMP / "vault", "Dasha")
    engine = FakeEngine()
    lib = library.Library({**CFG, "library": {"chunk_tokens": 200, "overlap": 0}}, db, FakeEmbedder(), vault, engine)
    brain = Brain(CFG, db, FakeLLM(), mem, FakeEmbedder(), vault, engine, areas, lib)
    lib.on_document.append(brain.on_document)
    ln = links.Links(CFG, db, lib, brain, engine)
    ln.transport = httpx.MockTransport(web)
    brain.fetch_url = ln.fetch
    links._download_audio = fake_download

    # an article: one item, its page in the library, the snapshot kept
    item = await brain.add("for_later", "example.com/article", url="https://example.com/article", fmt="link")
    await brain.fetch_url(item, item["url"])
    await asyncio.gather(*ln.tasks)
    await asyncio.gather(*lib.tasks)
    item = brain.get(item["id"])
    assert item["doc_id"] and len(brain.items(status="all")) == 1, brain.items(status="all")
    assert item["title"] == "Cómo regar con un Arduino" and item["format"] == "link", item
    text = lib.section_text(lib.sections(item["doc_id"])[0]["id"]) if lib.sections(item["doc_id"]) else ""
    full = " ".join(c["text"] for c in lib.chunks(item["doc_id"]))
    assert "capacitivo" in full and "Suscríbete" not in full and "cookies" not in full, full[:300]
    assert (lib.dir / "snapshots" / f"{item['doc_id']}.html").exists()
    assert not any("is in the library" in t for t in engine.said()), "the item is the news, no 📚 book message"
    await lib.run()
    pub = brain.public(brain.get(item["id"]))
    assert pub["doc"]["state"] == "ready" and any(e["type"] == "item" and e["item"]["doc"] and
                                                  e["item"]["doc"]["state"] == "ready" for e in engine.events)

    # a login wall and an empty JavaScript page: the link stays, a notice says so
    for path in ("private", "empty"):
        it = await brain.add("saved", path, url=f"https://example.com/{path}", fmt="link")
        await brain.fetch_url(it, it["url"])
        await asyncio.gather(*ln.tasks)
        it = brain.get(it["id"])
        assert it["doc_id"] is None and it["url"], it
    notes = [t for t in engine.said() if t.startswith("📚 I couldn't read the page")]
    assert len(notes) == 2 and "login" in notes[0] and "too little text" in notes[1], notes

    # a link to a PDF: the PDF itself
    it = await brain.add("saved", "paper", url="https://example.com/paper.pdf", fmt="link")
    await brain.fetch_url(it, it["url"])
    await asyncio.gather(*ln.tasks)
    await asyncio.gather(*lib.tasks)
    assert lib.document(brain.get(it["id"])["doc_id"])["kind"] == "pdf"

    # a reel: waits for the job, then caption + transcript
    reel = await brain.add("saved", "instagram.com/reel/X", url="https://www.instagram.com/reel/X/", fmt="video")
    await brain.fetch_url(reel, reel["url"])
    assert not ln.tasks and ln.pending(), "videos wait for quiet time"
    await ln.run()
    await asyncio.gather(*lib.tasks)
    reel = brain.get(reel["id"])
    full = " ".join(c["text"] for c in lib.chunks(reel["doc_id"]))
    assert "Ramen casero" in full and "ramen en casa" in full and reel["format"] == "video", full
    assert reel["title"] == "Ramen casero 🍜", reel["title"]
    private = await brain.add("saved", "ig private", url="https://www.instagram.com/reel/private/", fmt="video")
    await ln.run()
    assert not ln.pending() and brain.get(private["id"])["doc_id"] is None
    assert any(t.startswith("📚 I couldn't watch the video") and "login" in t for t in engine.said())
    print("ok links (article, snapshot, title, no extra item, login wall, empty page, pdf link, reel, private reel)")


def test_links():
    asyncio.run(main())


if __name__ == "__main__":
    test_links()
