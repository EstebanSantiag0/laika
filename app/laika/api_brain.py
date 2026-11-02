"""The second brain's API for laika-app (SB7 + SB7b; contract in docs/PLAN-second-brain.md §5 and docs/API.md).

Items (capture, list, edit, resurface, original file, rendered PDF pages), search over items and the
library, likes/dislikes (facts with a polarity), tags, and editing the whole graph (nodes, links, the
trash). Errors are JSON: {"error": ...} plus `field` (422), `max_mb` (413) or `node` (409).
"""
import asyncio
import json
import mimetypes
import re
import struct
import time
import urllib.parse
import zlib

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import lang, words
from .brain import KINDS, normalize_url, url_format
from .graph_edit import ApiError, POLARITIES

LIST_LIMIT, LIST_MAX = 30, 100
SEARCH_LIMIT = 20
RRF_K = 60
SNIPPET = 200
RESURFACE_MAX = 3
MEDIA = {"pdf": "application/pdf", "epub": "application/epub+zip", "fb2": "application/xml", "zip": "application/zip",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "html": "text/html",
         "txt": "text/plain; charset=utf-8", "md": "text/markdown; charset=utf-8"}
def png(width, height, rgb, stride):
    """A minimal PNG encoder (8-bit RGB), so rendering a page needs no image library."""
    raw = b"".join(b"\x00" + bytes(rgb[y * stride:y * stride + width * 3]) for y in range(height))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _int(value, name, lo, hi, default):
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be an integer") from None
    if not lo <= v <= hi:
        raise ApiError(400, f"{name} must be from {lo} to {hi}")
    return v


class BrainApi:
    def __init__(self, cfg, db, memory, embedder, brain, library, edit, graph_api):
        self.cfg, self.db, self.mem, self.embedder = cfg, db, memory, embedder
        self.brain, self.library, self.edit, self.graph = brain, library, edit, graph_api
        self.max_mb = (cfg.get("library") or {}).get("max_upload_mb", 100)
        self.tasks = set()
        self.router = APIRouter()
        r = self.router
        r.add_api_route("/api/items", self.create, methods=["POST"])
        r.add_api_route("/api/items", self.list, methods=["GET"])
        r.add_api_route("/api/items/{item_id}", self.get, methods=["GET"])
        r.add_api_route("/api/items/{item_id}", self.patch_item, methods=["PATCH"])
        r.add_api_route("/api/items/{item_id}/resurface", self.resurface_action, methods=["POST"])
        r.add_api_route("/api/items/{item_id}/file", self.file, methods=["GET"])
        r.add_api_route("/api/items/{item_id}/pages/{page}.png", self.page_png, methods=["GET"])
        r.add_api_route("/api/search", self.search, methods=["GET"])
        r.add_api_route("/api/resurface", self.resurface_list, methods=["GET"])
        r.add_api_route("/api/tags", self.tags, methods=["GET"])
        r.add_api_route("/api/likes", self.likes, methods=["GET"])
        r.add_api_route("/api/likes/{fact_id}", self.patch_like, methods=["PATCH"])
        r.add_api_route("/api/nodes/{node_id}", self.patch_node, methods=["PATCH"])
        r.add_api_route("/api/nodes/{node_id}", self.delete_node, methods=["DELETE"])
        r.add_api_route("/api/edges", self.add_edge, methods=["POST"])
        r.add_api_route("/api/edges/{edge_id}", self.patch_edge, methods=["PATCH"])
        r.add_api_route("/api/edges/{edge_id}", self.delete_edge, methods=["DELETE"])
        r.add_api_route("/api/trash", self.trash, methods=["GET"])
        r.add_api_route("/api/trash/edges/{edge_id}/restore", self.restore_edge, methods=["POST"])
        r.add_api_route("/api/trash/{node_id}/restore", self.restore_node, methods=["POST"])

    def _item(self, item_id):
        item = self.brain.get(item_id)
        if not item:
            raise ApiError(404, f"no such item {item_id}")
        return item

    def out(self, item):
        return self.brain.public(item)

    # items -------------------------------------------------------------------------------------------------
    async def create(self, request: Request):
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ApiError(400, "multipart needs a file field")
            data = await upload.read()
            if len(data) > self.max_mb * 2**20:
                raise ApiError(413, f"the file is over {self.max_mb} MB", max_mb=self.max_mb)
            if not data:
                raise ApiError(400, "the file is empty")
            opts = {"kind": form.get("kind"), "note": form.get("note"),
                    "comment": str(form.get("comment") or "").lower() in ("true", "1", "yes"),
                    "areas": [a.strip() for a in (form.get("areas") or "").split(",") if a.strip()] or None,
                    "tags": [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()] or None}
            return self._comment(opts, await self._create_file(data, upload.filename or "file", opts))
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "send multipart with a file, or JSON with url or text") from None
        if not isinstance(body, dict):
            raise ApiError(400, "the JSON body must be an object")
        opts = {k: body.get(k) for k in ("kind", "note", "areas", "tags")}
        opts["comment"] = body.get("comment") is True
        if isinstance(body.get("url"), str) and body["url"].strip():
            return self._comment(opts, await self._create_url(body["url"].strip(), opts))
        if isinstance(body.get("text"), str) and body["text"].strip():
            return self._comment(opts, await self._create_text(body["text"].strip(), opts))
        raise ApiError(400, "send multipart with a file, or JSON with url or text")

    def _check_opts(self, opts):
        if opts.get("kind") is not None and opts["kind"] not in KINDS:
            raise ApiError(422, f"kind must be one of {list(KINDS)}", field="kind")
        known = set(self.brain.areas.ids()) if self.brain.areas else set()
        if opts.get("areas") is not None and (not isinstance(opts["areas"], list) or len(opts["areas"]) > 3
                                              or any(a not in known for a in opts["areas"])):
            raise ApiError(422, "areas must be a list of at most 3 area ids", field="areas")
        if opts.get("tags") is not None and (not isinstance(opts["tags"], list) or len(opts["tags"]) > 3):
            raise ApiError(422, "tags must be a list of at most 3 strings", field="tags")
        if opts.get("note") is not None and not isinstance(opts["note"], str):
            raise ApiError(422, "note must be a string", field="note")

    async def _apply_opts(self, item, opts):
        fields = {k: opts[k] for k in ("kind", "note", "tags", "areas") if opts.get(k) is not None}
        if fields:
            item = await self.brain.update(item["id"], **fields)
        return item

    def _comment(self, opts, response):
        """comment=true: Laika takes a turn about it in the background; the HTTP answer doesn't wait."""
        if opts.get("comment"):
            item_id = json.loads(response.body)["item"]["id"]
            task = asyncio.create_task(self.brain.comment_on(item_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return response

    async def _create_file(self, data, filename, opts):
        self._check_opts(opts)
        doc, reply = await self.library.add(data, filename, "app", "web")
        if doc is None:
            raise ApiError(415, reply.removeprefix("📚 ") if reply else "this kind of file can't be read yet")
        item = self.brain.by_doc(doc["id"])
        if item is None:  # a document from before the second brain
            await self.brain.on_document("added", doc)
            item = self.brain.by_doc(doc["id"])
        if reply:  # already in the library
            return JSONResponse({"item": self.out(item), "duplicate": True}, status_code=200)
        item = await self._apply_opts(item, opts)
        return JSONResponse({"item": self.out(item), **({"restored": True} if doc.get("restored") else {})},
                            status_code=201)

    async def _create_url(self, url, opts):
        self._check_opts(opts)
        if not re.match(r"^https?://\S+$", url, re.I):
            raise ApiError(422, "url must start with http:// or https://", field="url")
        found = await self.brain.find_url(url)
        if found:
            item, restored = found
            if restored:  # it was in the trash: sending it again brings it back
                return JSONResponse({"item": self.out(await self._apply_opts(item, opts)), "restored": True},
                                    status_code=201)
            return JSONResponse({"item": self.out(item), "duplicate": True}, status_code=200)
        p = urllib.parse.urlsplit(url)
        item = await self.brain.add(opts.get("kind") or "saved", (p.netloc.removeprefix("www.") + p.path)[:120],
                                    "", fmt=url_format(url), url=url, note=opts.get("note") or "",
                                    areas=opts.get("areas") or [], tags=opts.get("tags") or [], channel="app")
        if self.brain.fetch_url:
            await self.brain.fetch_url(item, url)  # SB2/SB4: the page or the video goes into the library
        return JSONResponse({"item": self.out(self.brain.get(item["id"]))}, status_code=201)

    async def _create_text(self, text, opts):
        self._check_opts(opts)
        item = await self.brain.capture_text(text, kind=opts.get("kind"), areas=opts.get("areas"),
                                             tags=opts.get("tags"), note=opts.get("note") or "")
        return JSONResponse({"item": self.out(item)}, status_code=201)

    async def list(self, kind: str | None = None, area: str | None = None, tag: str | None = None,
                   status: str = "active", q: str | None = None, before: str | None = None,
                   limit: str | None = None):
        if kind is not None and kind not in KINDS:
            raise ApiError(422, f"kind must be one of {list(KINDS)}", field="kind")
        if status not in ("active", "done", "archived", "all"):
            raise ApiError(422, "status must be active, done, archived or all", field="status")
        lim = _int(limit, "limit", 1, LIST_MAX, LIST_LIMIT)
        cur = _int(before, "before", 1, 2**62, None)
        sql, args = "SELECT b.id FROM brain_items b WHERE b.trashed_at IS NULL", []
        if kind:
            sql, args = sql + " AND b.kind = ?", args + [kind]
        if status != "all":
            sql, args = sql + " AND b.status = ?", args + [status]
        if area:
            sql, args = sql + " AND EXISTS (SELECT 1 FROM area_links l WHERE l.kind = 'item' AND l.ref_id = b.id " \
                              "AND l.area = ?)", args + [area]
        if tag:
            sql, args = sql + " AND EXISTS (SELECT 1 FROM json_each(b.tags) t WHERE t.value = ?)", args + [tag]
        if cur:
            sql, args = sql + " AND b.id < ?", args + [cur]
        ids = [r[0] for r in self.db.conn.execute(sql + " ORDER BY b.id DESC", args)]
        if q:
            hits = {h for h in await self._item_hits(q)}
            ids = [i for i in ids if i in hits]
        page = ids[:lim]
        items = [self.out(self.brain.get(i)) for i in page]
        return {"items": items, "next_before": page[-1] if len(ids) > lim else None}

    async def get(self, item_id: int):
        return {"item": self.out(self._item(item_id))}

    async def patch_item(self, item_id: int, request: Request):
        self._item(item_id)
        body = await self._json(request)
        await self.edit.patch(f"item:{item_id}", body)
        return {"item": self.out(self.brain.get(item_id))}

    async def resurface_action(self, item_id: int, request: Request):
        item = self._item(item_id)
        action = (await self._json(request)).get("action")
        now = time.time()
        if action == "dismiss":
            await self.brain.update(item_id, resurface_at=None)
        elif action == "done":
            await self.brain.update(item_id, status="done", resurface_at=None)
        elif action == "later":
            self.brain.advance(item, now)  # the next step (3 → 10 → 30 days); after the last one it stops
            await self.brain.update(item_id)
        else:
            raise ApiError(422, "action must be dismiss, done or later", field="action")
        return {"item": self.out(self.brain.get(item_id))}

    def _doc(self, item):
        if not item["doc_id"] or not self.library or not (doc := self.library.document(item["doc_id"])):
            raise ApiError(404, "this item has no file")
        return doc

    async def file(self, item_id: int):
        doc = self._doc(self._item(item_id))
        path = self.library.original(doc)
        if not path.exists():
            raise ApiError(404, "the original file is missing")
        filename = json.loads(doc.get("meta") or "{}").get("filename") or f"{doc['title']}.{doc['kind']}"
        media = MEDIA.get(doc["kind"]) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=filename)

    async def page_png(self, item_id: int, page: int, w: str | None = None):
        doc = self._doc(self._item(item_id))
        if doc["kind"] != "pdf":
            raise ApiError(415, "only PDF pages can be rendered")
        width = _int(w, "w", 200, 2000, 1200)
        cache = self.library.dir / "pages" / f"{doc['id']}-{page}-{width}.png"
        if not cache.exists():
            data = await asyncio.to_thread(self._render_page, self.library.original(doc), page, width)
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(cache)
        return FileResponse(cache, media_type="image/png")

    @staticmethod
    def _render_page(path, page, width):
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            if not 1 <= page <= len(pdf):
                raise ApiError(404, f"the PDF has {len(pdf)} pages")
            pg = pdf[page - 1]
            bm = pg.render(scale=width / pg.get_width(), rev_byteorder=True)
            return png(bm.width, bm.height, bm.buffer, bm.stride)
        finally:
            pdf.close()

    # search --------------------------------------------------------------------------------------------------
    async def _item_hits(self, q):
        """Item ids for a query, best first: meaning (cosine ≥ 0.35) and words, fused."""
        ranks = {}
        try:
            vec = await self.embedder.one(q, query=True)
            vec_hits = [ref for _, ref, cos in self.mem.search_vec(vec, k=60, kinds=("brain",)) if cos >= 0.35]
        except Exception:
            vec_hits = []
        for lst in (vec_hits, [ref for _, ref, _ in self.mem.search_text(q, k=60, kinds=("brain",))]):
            for i, ref in enumerate(lst):
                ranks[ref] = ranks.get(ref, 0) + 1 / (RRF_K + i)
        return sorted(ranks, key=ranks.get, reverse=True)

    async def search(self, q: str = "", limit: str | None = None):
        if not q.strip():
            raise ApiError(400, "q is empty")
        lim = _int(limit, "limit", 1, LIST_MAX, SEARCH_LIMIT)
        scores, rows = {}, {}
        for i, ref in enumerate(await self._item_hits(q)):
            key = ("item", ref)
            scores[key] = scores.get(key, 0) + 1 / (RRF_K + i)
        docs = [d["id"] for d in self.library.searchable()] if self.library else []
        if docs:
            try:
                sims = self.library.similarities(await self.embedder.one(q, query=True), docs)
            except Exception:
                sims = {}
            for i, (cid, cos) in enumerate(sorted(sims.items(), key=lambda kv: kv[1], reverse=True)[:40]):
                if cos >= 0.35:
                    scores[("chunk", cid)] = scores.get(("chunk", cid), 0) + 1 / (RRF_K + i)
            terms = words.query_terms(q, lang.detect(q) or "es")
            for i, (cid, _) in enumerate(self.library.search_text(terms, docs, k=40)):
                scores[("chunk", cid)] = scores.get(("chunk", cid), 0) + 1 / (RRF_K + i)
        out = []
        for (kind, ref), score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
            if kind == "item":
                item = self.brain.get(ref)
                if not item:
                    continue
                text = item["text"] or item["title"]
                out.append({"item_id": item["id"], "doc_id": item["doc_id"], "chunk_id": None, "title": item["title"],
                            "kind": item["kind"], "format": item["format"], "snippet": _snippet(text, q), "text": text,
                            "page_from": None, "page_to": None, "score": round(score, 5)})
            else:
                c = self.library.chunk_row(ref)
                if not c:
                    continue
                item = self.brain.by_doc(c["doc_id"])
                doc = self.library.document(c["doc_id"])
                out.append({"item_id": item["id"] if item else None, "doc_id": c["doc_id"], "chunk_id": ref,
                            "title": item["title"] if item else doc["title"], "kind": item["kind"] if item else "saved",
                            "format": item["format"] if item else "text", "snippet": _snippet(c["text"], q),
                            "text": c["text"], "page_from": c.get("page_from"), "page_to": c.get("page_to"),
                            "score": round(score, 5)})
            if len(out) >= lim:
                break
        return {"results": out}

    # resurfacing, tags, likes ---------------------------------------------------------------------------------
    async def resurface_list(self):
        """Items due to come back (SB6 refines this nightly; this read is cheap and makes no model call)."""
        return {"items": [self.out(i) for i in self.brain.due(limit=RESURFACE_MAX)]}

    async def tags(self):
        rows = self.db.conn.execute("SELECT t.value AS tag, COUNT(*) AS n FROM brain_items b, json_each(b.tags) t "
                                    "WHERE b.trashed_at IS NULL GROUP BY t.value ORDER BY n DESC, t.value").fetchall()
        return [{"tag": r["tag"], "count": r["n"]} for r in rows]

    async def likes(self):
        rows = self.db.conn.execute("SELECT id FROM facts WHERE status = 'active' AND polarity IN ('like', 'dislike') "
                                    "ORDER BY recorded_at DESC").fetchall()
        out = {"likes": [], "dislikes": []}
        for r in rows:
            f = self.mem.get_fact(r[0])
            out["likes" if f["polarity"] == "like" else "dislikes"].append(self.graph.fact_node(f))
        return out

    async def patch_like(self, fact_id: int, request: Request):
        f = self.mem.get_fact(fact_id)
        if not f or f["status"] != "active":
            raise ApiError(404, f"no active fact {fact_id}")
        body = await self._json(request)
        if "polarity" in body:
            if body["polarity"] not in POLARITIES:
                raise ApiError(422, "polarity must be like, dislike or null", field="polarity")
            self.mem.update_fact(fact_id, polarity=body["polarity"])
        if "status" in body:
            if body["status"] != "archived":
                raise ApiError(422, "status can only be archived", field="status")
            self.mem.update_fact(fact_id, status="archived")
        if not {"polarity", "status"} & set(body):
            raise ApiError(422, "send polarity or status", field="polarity")
        return self.graph.fact_node(self.mem.get_fact(fact_id))

    # the graph ----------------------------------------------------------------------------------------------
    def _edges_of(self, node_id):
        g = self.graph.build_graph("all")
        return [e for e in g["edges"] if node_id in (e["source"], e["target"])]

    async def patch_node(self, node_id: str, request: Request, dry_run: bool = False):
        body = await self._json(request)
        result = await self.edit.patch(node_id, body, dry_run=dry_run)
        if dry_run:
            return result
        return {"node": self.graph.node(result), "edges": self._edges_of(result)}

    async def delete_node(self, node_id: str, with_facts: bool = False, dry_run: bool = False):
        return await self.edit.delete(node_id, with_facts=with_facts, dry_run=dry_run)

    async def add_edge(self, request: Request):
        body = await self._json(request)
        edge, dup = self.edit.add_link(body.get("source"), body.get("target"), body.get("label") or "")
        return JSONResponse({"edge": edge, "duplicate": True} if dup else {"edge": edge}, status_code=200 if dup else 201)

    async def patch_edge(self, edge_id: str, request: Request):
        return {"edge": self.edit.patch_link(edge_id, await self._json(request))}

    async def delete_edge(self, edge_id: str):
        return {"edges_removed": await self.edit.delete_edge(edge_id)}

    async def trash(self):
        return self.edit.trash(lambda nid: self.graph.node(nid, trashed=True))

    async def restore_node(self, node_id: str):
        restored = await self.edit.restore(node_id)
        edges = []
        for nid in restored:
            edges += [e for e in self._edges_of(nid) if e not in edges]
        return {"restored": restored, "edges": edges}

    async def restore_edge(self, edge_id: str):
        return {"edge": self.edit.restore_edge(edge_id)}

    @staticmethod
    async def _json(request):
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(400, "the body must be JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "the JSON body must be an object")
        return body


def _snippet(text, q):
    """~200 characters around the first query word found (or the start)."""
    text = " ".join((text or "").split())
    low = text.lower()
    pos = min((p for w in re.findall(r"\w{3,}", q.lower()) if (p := low.find(w)) >= 0), default=0)
    start = max(0, pos - SNIPPET // 3)
    out = text[start:start + SNIPPET]
    return ("…" if start else "") + out + ("…" if start + SNIPPET < len(text) else "")


def install_errors(app):
    """Errors are {"error": ...} everywhere (docs/API.md), not FastAPI's {"detail": ...}."""
    @app.exception_handler(ApiError)
    async def api_error(_request, exc: ApiError):
        return JSONResponse(exc.payload, status_code=exc.status)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request, exc: StarletteHTTPException):
        return JSONResponse({"error": exc.detail if isinstance(exc.detail, str) else str(exc.detail)},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        err = (exc.errors() or [{}])[0]
        loc = [str(x) for x in err.get("loc", []) if x not in ("body", "query", "path")]
        return JSONResponse({"error": err.get("msg", "invalid request"), "field": ".".join(loc) or None},
                            status_code=422)
