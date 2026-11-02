"""Edit the whole graph from the app (SB7b, docs/PLAN-second-brain.md §5, DECISIONS D23).

Every dot can be edited and deleted, every link added, relabeled, removed or hidden, and every delete
goes to a 30-day trash first, from which one restore brings back the dot and everything its delete
took with it. After 30 days the nightly job purges it for good (memories through `forget()`, with a
tombstone, so she doesn't learn them again). "Forget X" in the chat still deletes at once.

Ids are the graph's strings: nodes `fact:12`, `entity:51`, `episode:9`, `reminder:4`, `item:3`,
`area:study`, `tag:pasaporte`; stored links `link:42` (his) and `about:17` (an item's automatic link);
computed links `<kind>:<source>:<target>`, e.g. `similar:fact:12:fact:40`.

Where the trash lives:
- facts and reminders: status `trashed` (the previous status is kept in the trash row);
- entities, episodes, items: `trashed_at`;
- library documents of trashed items: status `trashed`;
- his links: `graph_links.trashed_at`; hidden automatic links: `graph_links` rows with action `hide`
  (listed in the trash, no purge date);
- the `trash` table: one row per delete, with what it took along (for one undo).
"""
import datetime
import json
import logging
import re
import time

log = logging.getLogger(__name__)

TRASH_DAYS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS trash (
    id INTEGER PRIMARY KEY,
    node TEXT NOT NULL,
    trashed_at REAL NOT NULL,
    purge_at REAL NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    restored_at REAL
);
CREATE INDEX IF NOT EXISTS trash_node ON trash(node);
"""
TIME_SCOPES = ("permanent", "stage", "dated", "archived")
POLARITIES = ("like", "dislike", None)
SUBKINDS = ("person", "place", "thing", "topic", "work")
ITEM_STATUSES = ("active", "done", "archived")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][\d:.+-]+)?$")
TAG_NAME = re.compile(r"^[^:\s][^:]{0,39}$")
COMPUTED = ("similar", "related", "about", "in_episode", "in_area", "tagged")

EDITABLE = {
    "item": ["title", "text", "note", "kind", "areas", "tags", "status"],
    "fact": ["text", "areas", "time_scope", "valid_from", "valid_to", "review_after", "polarity", "sensitive",
             "no_mention"],
    "entity": ["name", "aliases", "summary", "subkind"],
    "episode": ["title", "text"],
    "reminder": ["text", "due_at", "areas"],
    "area": ["name", "color"],
    "tag": ["name"],
}


class ApiError(Exception):
    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status, self.payload = status, {"error": error, **extra}


def parse_node(node_id):
    kind, sep, ref = (node_id or "").partition(":")
    if not sep or kind not in EDITABLE or not ref:
        raise ApiError(404, f"unknown node {node_id!r}")
    if kind in ("area", "tag"):
        return kind, ref
    try:
        return kind, int(ref)
    except ValueError:
        raise ApiError(404, f"unknown node {node_id!r}") from None


def parse_edge(edge_id):
    """('stored', kind, row id) or ('computed', kind, source, target)."""
    parts = (edge_id or "").split(":")
    if len(parts) == 2 and parts[0] in ("link", "about") and parts[1].isdigit():
        return "stored", parts[0], int(parts[1])
    if len(parts) == 5 and parts[0] in COMPUTED:
        return "computed", parts[0], f"{parts[1]}:{parts[2]}", f"{parts[3]}:{parts[4]}"
    raise ApiError(404, f"unknown edge {edge_id!r}")


class GraphEdit:
    def __init__(self, cfg, db, memory, embedder, vault, brain, reminders, areas, library=None, consolidator=None):
        self.cfg, self.db, self.mem, self.embedder, self.vault = cfg, db, memory, embedder, vault
        self.brain, self.reminders, self.areas, self.library, self.consolidator = brain, reminders, areas, library, consolidator
        self.conn = db.conn
        self.conn.executescript(SCHEMA)

    # looking things up ----------------------------------------------------------------------------------
    def row(self, node_id, trashed=False):
        """The node's database row (a dict), or None. Trashed ones only with trashed=True."""
        kind, ref = parse_node(node_id)
        c = self.conn
        if kind == "fact":
            r = self.mem.get_fact(ref)
            if not r or r["status"] == "rejected" or (r["status"] == "trashed" and not trashed):
                return None
            return r
        if kind == "entity":
            r = self.mem.get_entity(ref)
            return r if r and (trashed or not r.get("trashed_at")) else None
        if kind == "episode":
            r = self.mem.get_episode(ref)
            return r if r and (trashed or not r.get("trashed_at")) else None
        if kind == "reminder":
            r = self.reminders.get(ref)
            return r if r and (trashed or r["status"] != "trashed") else None
        if kind == "item":
            return self.brain.get(ref, trashed=trashed)
        if kind == "area":
            r = c.execute("SELECT * FROM areas WHERE id = ? AND active = 1", (ref,)).fetchone()
            return dict(r) if r else None
        if kind == "tag":
            return {"name": ref} if self.tag_items(ref) else None
        return None

    def exists(self, node_id):
        try:
            return self.row(node_id) is not None
        except ApiError:
            return False

    def tag_items(self, tag):
        return [r[0] for r in self.conn.execute(
            "SELECT b.id FROM brain_items b, json_each(b.tags) t WHERE t.value = ? AND b.trashed_at IS NULL", (tag,))]

    def updated_ts(self, node_id):
        kind, _ = parse_node(node_id)
        r = self.row(node_id, trashed=True)
        if not r:
            return None
        if kind == "fact":
            return r.get("updated") or r["recorded_at"]
        if kind == "entity":
            return r["updated"]
        if kind == "episode":
            return r.get("updated") or r["created"]
        if kind == "reminder":
            return r.get("updated") or r["created"]
        if kind == "item":
            return r["updated"]
        return None

    # editing ---------------------------------------------------------------------------------------------
    async def patch(self, node_id, body, dry_run=False):
        """Change a node's fields. Raises ApiError (404, 409 stale, 422 bad field). Returns the node id (a tag
        rename returns the new id); with dry_run (tags only) returns {merges_into, nodes_affected}."""
        kind, ref = parse_node(node_id)
        if self.row(node_id) is None:
            raise ApiError(404, f"no such node {node_id}")
        body = dict(body or {})
        base = body.pop("base_updated_at", None)
        if base is not None and kind not in ("area", "tag"):
            try:  # compared as instants (SB8: a client re-serialized "…-06:00" as "…Z" and got a false 409)
                base_ts = datetime.datetime.fromisoformat(str(base).replace("Z", "+00:00")).timestamp()
            except ValueError:
                raise ApiError(422, "base_updated_at must be an ISO datetime", field="base_updated_at") from None
            current = self.updated_ts(node_id)
            if current and int(current) != int(base_ts):  # updated_at is shown to the second
                raise ApiError(409, "changed since you loaded it", node_id=node_id)
        for field in body:
            if field not in EDITABLE[kind]:
                raise ApiError(422, f"{field} can't be edited on a {kind}", field=field)
        if dry_run and kind != "tag":
            raise ApiError(400, "dry_run is only for tag renames")
        handler = getattr(self, f"_patch_{kind}")
        return await handler(ref, body, dry_run) if kind == "tag" else await handler(ref, body)

    def _text(self, body, field, limit, required=True):
        v = body[field]
        if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
            raise ApiError(422, f"{field} must be a{' non-empty' if required else ''} string of at most {limit} "
                                f"characters", field=field)
        return v.strip()

    def _areas(self, body, field="areas", limit=3):
        v = body[field]
        known = set(self.areas.ids()) if self.areas else set()
        if not isinstance(v, list) or len(v) > limit or any(a not in known for a in v):
            raise ApiError(422, f"{field} must be a list of at most {limit} area ids", field=field)
        return list(dict.fromkeys(v))

    def _enum(self, body, field, options):
        if body[field] not in options:
            raise ApiError(422, f"{field} must be one of {[o for o in options]}", field=field)
        return body[field]

    def _date(self, body, field):
        v = body[field]
        if v is not None and (not isinstance(v, str) or not DATE.match(v)):
            raise ApiError(422, f"{field} must be a date (YYYY-MM-DD) or null", field=field)
        return v

    async def _patch_item(self, ref, body):
        fields = {}
        if "title" in body:
            fields["title"], fields["title_locked"] = self._text(body, "title", 120), 1
        for f, n in (("text", 20000), ("note", 5000)):
            if f in body:
                fields[f] = self._text(body, f, n, required=False)
        if "kind" in body:
            fields["kind"] = self._enum(body, "kind", ("idea", "for_later", "saved"))
        if "status" in body:
            fields["status"] = self._enum(body, "status", ITEM_STATUSES)
        if "tags" in body:
            tags = body["tags"]
            if not isinstance(tags, list) or len(tags) > 3 or any(not isinstance(t, str) for t in tags):
                raise ApiError(422, "tags must be a list of at most 3 strings", field="tags")
            fields["tags"] = tags
        if "areas" in body:
            fields["areas"] = self._areas(body)
        await self.brain.update(ref, **fields)
        return f"item:{ref}"

    async def _patch_fact(self, ref, body):
        f = self.mem.get_fact(ref)
        fields, vec = {}, None
        if "text" in body:
            fields["text"] = self._text(body, "text", 500)
            fields["origin"] = "edited"
            vec = await self.embedder.one(fields["text"])
        if "time_scope" in body:
            scope = self._enum(body, "time_scope", TIME_SCOPES)
            if scope == "archived":
                fields["status"] = "archived"
            else:
                fields["time_scope"] = scope
                if f["status"] == "archived":
                    fields["status"] = "active"
        for d in ("valid_from", "valid_to", "review_after"):
            if d in body:
                fields[d] = self._date(body, d)
        if "polarity" in body:
            fields["polarity"] = self._enum(body, "polarity", POLARITIES)
        for b in ("sensitive", "no_mention"):
            if b in body:
                if not isinstance(body[b], bool):
                    raise ApiError(422, f"{b} must be true or false", field=b)
                fields[b] = int(body[b])
        if "areas" in body and self.areas:
            self.areas.set("fact", ref, self._areas(body))
        self.mem.update_fact(ref, embedding=vec, **fields)
        if "text" in fields:
            self._rerender_fact(ref)
        return f"fact:{ref}"

    async def _patch_entity(self, ref, body):
        e = self.mem.get_entity(ref)
        fields = {}
        if "name" in body:
            name = self._text(body, "name", 80)
            clash = self.conn.execute("SELECT id FROM entities WHERE name = ? COLLATE NOCASE AND id != ?",
                                      (name, ref)).fetchone()
            if clash:
                raise ApiError(422, f"another dot is already called {name!r}", field="name")
            fields["name"] = name
        if "aliases" in body:
            al = body["aliases"]
            if not isinstance(al, list) or any(not isinstance(a, str) or not a.strip() or len(a) > 80 for a in al):
                raise ApiError(422, "aliases must be a list of strings", field="aliases")
            fields["aliases"] = json.dumps(sorted({a.strip() for a in al}), ensure_ascii=False)
        if "summary" in body:
            fields["summary"] = self._text(body, "summary", 5000, required=False)
        if "subkind" in body:
            fields["kind"] = self._enum(body, "subkind", SUBKINDS)
        if fields:
            fields["updated"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE entities SET {cols} WHERE id = ?", (*fields.values(), ref))
        if self.consolidator:
            if "name" in fields and e.get("note_path"):  # the note follows the new name
                self.vault.trash_note(e["note_path"])
                self.conn.execute("UPDATE entities SET note_path = NULL WHERE id = ?", (ref,))
            self.consolidator.render_entity(ref)
        return f"entity:{ref}"

    async def _patch_episode(self, ref, body):
        fields = {}
        if "title" in body:
            fields["title"] = self._text(body, "title", 120)
        if "text" in body:
            fields["narrative"] = self._text(body, "text", 5000)
        if fields:
            fields["updated"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE episodes SET {cols} WHERE id = ?", (*fields.values(), ref))
            ep = self.mem.get_episode(ref)
            try:
                vec = await self.embedder.one(f"{ep['title']}. {ep['narrative']}")
            except Exception:
                vec = None
            self.mem._index("episode", ref, f"{ep['title']}. {ep['narrative']}", vec)
            self._render_episode_day(ep)
        return f"episode:{ref}"

    async def _patch_reminder(self, ref, body):
        r = self.reminders.get(ref)
        sets = {}
        if "text" in body:
            sets["text"] = self._text(body, "text", 300)
        if "due_at" in body:
            try:
                due = datetime.datetime.fromisoformat(body["due_at"])
                if due.tzinfo is None:
                    due = due.replace(tzinfo=self.brain.tz)
            except (TypeError, ValueError):
                raise ApiError(422, "due_at must be an ISO datetime", field="due_at") from None
            sets["due_at"] = due.timestamp()
            if r["status"] in ("delivered", "snoozed"):
                sets["status"] = "pending"
        if "areas" in body:
            sets["areas"] = json.dumps(self._areas(body, limit=2))
        if sets:
            sets["updated"] = time.time()
            cols = ", ".join(f"{k} = ?" for k in sets)
            self.conn.execute(f"UPDATE reminders SET {cols} WHERE id = ?", (*sets.values(), ref))
        return f"reminder:{ref}"

    async def _patch_area(self, ref, body):
        name = self._text(body, "name", 40) if "name" in body else None
        color = body.get("color")
        if "color" in body and (not isinstance(color, str) or not HEX.match(color)):
            raise ApiError(422, "color must be #rrggbb", field="color")
        overrides = self.db.get_setting("area_overrides") or {}
        o = overrides.get(ref, {})
        if name:
            o["name"] = name
        if color:
            o["color"] = color
        overrides[ref] = o
        self.db.set_setting("area_overrides", overrides)
        self.areas.apply_overrides()
        return f"area:{ref}"

    async def _patch_tag(self, ref, body, dry_run=False):
        new = body.get("name")
        if not isinstance(new, str) or not TAG_NAME.match(new.strip().lower()):
            raise ApiError(422, "name must be at most 40 characters, without ':'", field="name")
        from .brain import clean_tags
        new = (clean_tags([new]) or [""])[0]
        if not new:
            raise ApiError(422, "name must have letters or digits", field="name")
        items = self.tag_items(ref)
        merges = new if new != ref and self.tag_items(new) else None
        if dry_run:
            return {"merges_into": merges, "nodes_affected": len(items)}
        if new == ref:
            return f"tag:{ref}"
        self._trash_row(f"tag:{ref}", {"op": "rename", "items": items, "to": new})
        for iid in items:
            item = self.brain.get(iid)
            tags = [new if t == ref else t for t in item["tags"]]
            await self.brain.update(iid, tags=list(dict.fromkeys(tags)))
        return f"tag:{new}"

    # deleting ---------------------------------------------------------------------------------------------
    async def delete(self, node_id, with_facts=False, dry_run=False):
        kind, ref = parse_node(node_id)
        if kind == "area":
            raise ApiError(409, "the life areas can't be deleted, only renamed or recolored")
        if self.row(node_id) is None:
            raise ApiError(404, f"no such node {node_id}")
        trashed, edges = self._cascade(kind, ref, with_facts)
        if dry_run:
            return {"trashed": trashed, "edges_removed": edges}
        payload = {"with": trashed[1:], "links": [], "with_facts": bool(with_facts)}
        now = time.time()
        for nid in trashed:
            k, r = parse_node(nid)
            payload.setdefault("prev", {})[nid] = await self._trash_one(k, r, now)
        links = [int(e.split(":")[1]) for e in edges if e.startswith("link:")]
        for lid in links:  # his own links to a trashed dot go with it (and come back with it)
            self.conn.execute("UPDATE graph_links SET trashed_at = ? WHERE id = ?", (now, lid))
        payload["links"] = links
        self._trash_row(node_id, payload)
        log.info("graph: %s moved to the trash (%d with it)", node_id, len(trashed) - 1)
        return {"trashed": trashed, "edges_removed": edges}

    def _cascade(self, kind, ref, with_facts):
        node = f"{kind}:{ref}"
        trashed = [node]
        if kind == "entity" and with_facts:
            trashed += [f"fact:{f['id']}" for f in self.mem.facts(ref, statuses=("active", "tentative", "archived"))]
        edges = []
        nodes = set(trashed)
        for r in self.conn.execute("SELECT id, kind, source, target FROM graph_links WHERE action = 'add' AND "
                                   "trashed_at IS NULL"):
            if r["source"] in nodes or r["target"] in nodes:
                edges.append(f"{r['kind']}:{r['id']}")
        if kind == "tag":
            edges += [f"tagged:item:{i}:tag:{ref}" for i in self.tag_items(ref)]
        return trashed, edges

    async def _trash_one(self, kind, ref, now):
        """Hide one node; returns what a restore needs."""
        c = self.conn
        if kind == "fact":
            f = self.mem.get_fact(ref)
            self.mem.update_fact(ref, status="trashed")
            self.vault.drop_fact(ref)
            return {"status": f["status"]}
        if kind == "entity":
            e = self.mem.get_entity(ref)
            c.execute("UPDATE entities SET trashed_at = ? WHERE id = ?", (now, ref))
            moved = self.vault.trash_note(e["note_path"]) if e.get("note_path") else None
            return {"note": e.get("note_path") if moved else None}
        if kind == "episode":
            ep = self.mem.get_episode(ref)
            c.execute("UPDATE episodes SET trashed_at = ? WHERE id = ?", (now, ref))
            self._render_episode_day(ep)
            return {}
        if kind == "reminder":
            r = self.reminders.get(ref)
            c.execute("UPDATE reminders SET status = 'trashed', updated = ? WHERE id = ?", (now, ref))
            return {"status": r["status"]}
        if kind == "item":
            item = self.brain.get(ref)
            c.execute("UPDATE brain_items SET trashed_at = ? WHERE id = ?", (now, ref))
            moved = self.vault.trash_note(item["note_path"]) if item["note_path"] else None
            doc_status = None
            if item["doc_id"] and self.library and (doc := self.library.document(item["doc_id"])) and not c.execute(
                    "SELECT 1 FROM brain_items WHERE doc_id = ? AND id != ? AND trashed_at IS NULL",
                    (item["doc_id"], ref)).fetchone():
                doc_status = doc["status"]
                c.execute("UPDATE lib.documents SET status = 'trashed' WHERE id = ?", (doc["id"],))
            return {"note": item["note_path"] if moved else None, "doc_status": doc_status}
        if kind == "tag":
            items = self.tag_items(ref)
            for iid in items:
                item = self.brain.get(iid)
                await self.brain.update(iid, tags=[t for t in item["tags"] if t != ref])
            return {"items": items}
        return {}

    def _trash_row(self, node_id, payload):
        now = time.time()
        self.conn.execute("INSERT INTO trash(node, trashed_at, purge_at, payload) VALUES (?, ?, ?, ?)",
                          (node_id, now, now + TRASH_DAYS * 86400, json.dumps(payload)))

    # restoring ---------------------------------------------------------------------------------------------
    async def restore(self, node_id):
        row = self.conn.execute("SELECT * FROM trash WHERE node = ? AND restored_at IS NULL ORDER BY id DESC LIMIT 1",
                                (node_id,)).fetchone()
        if not row:
            raise ApiError(404, f"{node_id} is not in the trash")
        payload = json.loads(row["payload"])
        restored = []
        if payload.get("op") == "rename":  # undo a tag rename or merge
            old = node_id.split(":", 1)[1]
            for iid in payload["items"]:  # the items that had the old tag get it back instead of the new one
                item = self.brain.get(iid)
                if item:
                    await self.brain.update(iid, tags=list(dict.fromkeys(
                        old if t == payload["to"] else t for t in item["tags"])))
            restored = [node_id]
        else:
            for nid, prev in (payload.get("prev") or {}).items():
                k, r = parse_node(nid)
                await self._restore_one(k, r, prev)
                restored.append(nid)
            for lid in payload.get("links", []):
                self.conn.execute("UPDATE graph_links SET trashed_at = NULL WHERE id = ?", (lid,))
        self.conn.execute("UPDATE trash SET restored_at = ? WHERE id = ?", (time.time(), row["id"]))
        log.info("graph: %s restored from the trash", node_id)
        return restored

    async def _restore_one(self, kind, ref, prev):
        c = self.conn
        if kind == "fact":
            self.mem.update_fact(ref, status=prev.get("status") or "active")
            self._rerender_fact(ref)
        elif kind == "entity":
            c.execute("UPDATE entities SET trashed_at = NULL, updated = ? WHERE id = ?", (time.time(), ref))
            if prev.get("note"):
                self.vault.untrash_note(prev["note"])
            if self.consolidator:
                self.consolidator.render_entity(ref)
        elif kind == "episode":
            c.execute("UPDATE episodes SET trashed_at = NULL WHERE id = ?", (ref,))
            self._render_episode_day(self.mem.get_episode(ref))
        elif kind == "reminder":
            c.execute("UPDATE reminders SET status = ?, updated = ? WHERE id = ?",
                      (prev.get("status") or "pending", time.time(), ref))
        elif kind == "item":
            c.execute("UPDATE brain_items SET trashed_at = NULL, updated = ? WHERE id = ?", (time.time(), ref))
            if prev.get("note"):
                self.vault.untrash_note(prev["note"])
            item = self.brain.get(ref)
            if prev.get("doc_status") and item and item["doc_id"]:
                c.execute("UPDATE lib.documents SET status = ? WHERE id = ?", (prev["doc_status"], item["doc_id"]))
            self.brain.render(ref)
        elif kind == "tag":
            for iid in prev.get("items", []):
                item = self.brain.get(iid)
                if item and ref not in item["tags"]:
                    await self.brain.update(iid, tags=item["tags"] + [ref])

    # links ----------------------------------------------------------------------------------------------------
    def edge_out(self, row):
        return {"id": f"{row['kind']}:{row['id']}", "source": row["source"], "target": row["target"],
                "kind": row["kind"], "weight": 1.0, "origin": "user" if row["origin"] == "user" else "auto",
                "label": row["label"]}

    def add_link(self, source, target, label=""):
        if not isinstance(label, str) or len(label) > 80:
            raise ApiError(422, "label must be a string of at most 80 characters", field="label")
        for field, node in (("source", source), ("target", target)):
            if not isinstance(node, str) or not self.exists(node):
                raise ApiError(422, f"{field} {node!r} doesn't exist or is in the trash", field=field)
        if source == target:
            raise ApiError(422, "a dot can't link to itself", field="target")
        old = self.conn.execute("SELECT * FROM graph_links WHERE kind = 'link' AND action = 'add' AND trashed_at IS NULL "
                                "AND ((source = ? AND target = ?) OR (source = ? AND target = ?))",
                                (source, target, target, source)).fetchone()
        if old:
            return self.edge_out(old), True
        now = time.time()
        cur = self.conn.execute("INSERT INTO graph_links(source, target, kind, label, origin, action, created, updated) "
                                "VALUES (?, ?, 'link', ?, 'user', 'add', ?, ?)", (source, target, label.strip(), now, now))
        self._touch(source, target)
        return self.edge_out(self.conn.execute("SELECT * FROM graph_links WHERE id = ?", (cur.lastrowid,)).fetchone()), False

    def patch_link(self, edge_id, body):
        kind_, *rest = parse_edge(edge_id)
        if kind_ != "stored":
            raise ApiError(422, "only links you drew or an item's links have a label", field="label")
        row = self.conn.execute("SELECT * FROM graph_links WHERE id = ? AND kind = ? AND action = 'add' AND "
                                "trashed_at IS NULL", (rest[1], rest[0])).fetchone()
        if not row:
            raise ApiError(404, f"no such edge {edge_id}")
        label = body.get("label", "")
        if not isinstance(label, str) or len(label) > 80:
            raise ApiError(422, "label must be a string of at most 80 characters", field="label")
        self.conn.execute("UPDATE graph_links SET label = ?, updated = ? WHERE id = ?", (label.strip(), time.time(), row["id"]))
        self._touch(row["source"], row["target"])
        return self.edge_out(self.conn.execute("SELECT * FROM graph_links WHERE id = ?", (row["id"],)).fetchone())

    async def delete_edge(self, edge_id):
        parsed = parse_edge(edge_id)
        now = time.time()
        if parsed[0] == "stored":
            _, kind, rid = parsed
            row = self.conn.execute("SELECT * FROM graph_links WHERE id = ? AND kind = ? AND action = 'add' AND "
                                    "trashed_at IS NULL", (rid, kind)).fetchone()
            if not row:
                raise ApiError(404, f"no such edge {edge_id}")
            if kind == "link":  # his own link: to the trash
                self.conn.execute("UPDATE graph_links SET trashed_at = ? WHERE id = ?", (now, rid))
            else:  # an item's automatic link: hidden, so filing never makes it again
                self.conn.execute("UPDATE graph_links SET action = 'hide', origin = 'hidden-auto', updated = ? "
                                  "WHERE id = ?", (now, rid))
            self._touch(row["source"], row["target"])
            return [edge_id]
        _, kind, source, target = parsed
        for node in (source, target):
            if not self.exists(node):
                raise ApiError(404, f"no such edge {edge_id}")
        if kind == "in_area" and source.split(":", 1)[0] in ("fact", "item", "reminder"):
            k, r = parse_node(source)  # areas are a field of these: removing the link edits the field
            area = target.split(":", 1)[1]
            current = self.areas.of(k, r)
            if area not in current:
                raise ApiError(404, f"no such edge {edge_id}")
            await self.patch(source, {"areas": [a for a in current if a != area]})
            return [edge_id]
        if kind == "tagged":
            _, r = parse_node(source)
            item = self.brain.get(r)
            tag = target.split(":", 1)[1]
            if not item or tag not in item["tags"]:
                raise ApiError(404, f"no such edge {edge_id}")
            await self.brain.update(r, tags=[t for t in item["tags"] if t != tag])
            return [edge_id]
        if not self.brain.hidden(source, target, kind):
            self.conn.execute("INSERT INTO graph_links(source, target, kind, origin, action, created, updated) "
                              "VALUES (?, ?, ?, 'user', 'hide', ?, ?)", (source, target, kind, now, now))
        return [edge_id]

    def restore_edge(self, edge_id):
        parsed = parse_edge(edge_id)
        if parsed[0] == "stored":
            _, kind, rid = parsed
            row = self.conn.execute("SELECT * FROM graph_links WHERE id = ? AND kind = ?", (rid, kind)).fetchone()
            if not row or (row["action"] == "add" and row["trashed_at"] is None):
                raise ApiError(404, f"{edge_id} is not removed or hidden")
            self.conn.execute("UPDATE graph_links SET action = 'add', trashed_at = NULL, updated = ?, origin = CASE "
                              "WHEN origin = 'hidden-auto' THEN 'auto' ELSE origin END WHERE id = ?", (time.time(), rid))
            row = self.conn.execute("SELECT * FROM graph_links WHERE id = ?", (rid,)).fetchone()
            return self.edge_out(row)
        _, kind, source, target = parsed
        cur = self.conn.execute("DELETE FROM graph_links WHERE action = 'hide' AND kind = ? AND "
                                "((source = ? AND target = ?) OR (source = ? AND target = ?))",
                                (kind, source, target, target, source))
        if not cur.rowcount:
            raise ApiError(404, f"{edge_id} is not hidden")
        return {"id": edge_id, "source": source, "target": target, "kind": kind, "weight": 1.0,
                "origin": "computed", "label": ""}

    def _touch(self, *nodes):
        """A link changed: items' notes list their links."""
        for n in nodes:
            if n.startswith("item:"):
                self.brain.render(int(n.split(":", 1)[1]))

    # the trash -------------------------------------------------------------------------------------------------
    def trash(self, graph_node):
        """{nodes, hidden_edges} for GET /api/trash. `graph_node(node_id)` gives a node in the graph's format."""
        nodes = []
        for r in self.conn.execute("SELECT * FROM trash WHERE restored_at IS NULL AND purge_at > ? ORDER BY id DESC",
                                   (time.time(),)):
            payload = json.loads(r["payload"])
            n = graph_node(r["node"]) if payload.get("op") != "rename" else None
            n = n or {"id": r["node"], "kind": r["node"].split(":", 1)[0],
                      "label": r["node"].split(":", 1)[1] if payload.get("op") == "rename" else r["node"]}
            nodes.append({**n, "trashed_at": self.brain.iso(r["trashed_at"]), "purge_at": self.brain.iso(r["purge_at"]),
                          "with": payload.get("with", []),
                          **({"renamed_to": payload["to"]} if payload.get("op") == "rename" else {})})
        hidden = []
        for r in self.conn.execute("SELECT * FROM graph_links WHERE (action = 'hide') OR (kind = 'link' AND trashed_at IS "
                                   "NOT NULL) ORDER BY updated DESC"):
            if r["origin"] == "hidden-auto":  # an item's automatic link he hid
                eid = f"about:{r['id']}"
            elif r["kind"] == "link":
                eid = f"link:{r['id']}"
            else:
                eid = f"{r['kind']}:{r['source']}:{r['target']}"
            hidden.append({"id": eid, "source": r["source"], "target": r["target"], "kind": r["kind"],
                           "label": r["label"], "hidden_at": self.brain.iso(r["trashed_at"] or r["updated"])})
        return {"nodes": nodes, "hidden_edges": hidden}

    async def purge(self, now=None):
        """Nightly: what has been in the trash for 30 days goes for good."""
        now = now or time.time()
        n = 0
        for r in self.conn.execute("SELECT * FROM trash WHERE restored_at IS NULL AND purge_at <= ?", (now,)).fetchall():
            payload = json.loads(r["payload"])
            if payload.get("op") != "rename":
                for nid in payload.get("prev", {}):
                    await self._purge_one(*parse_node(nid))
                for lid in payload.get("links", []):
                    self.conn.execute("DELETE FROM graph_links WHERE id = ?", (lid,))
            self.conn.execute("DELETE FROM trash WHERE id = ?", (r["id"],))
            n += 1
        for r in self.conn.execute("SELECT id FROM graph_links WHERE kind = 'link' AND trashed_at IS NOT NULL AND trashed_at "
                                   "<= ?", (now - TRASH_DAYS * 86400,)).fetchall():
            self.conn.execute("DELETE FROM graph_links WHERE id = ?", (r["id"],))
        if n:
            log.info("graph: purged %d trash entries older than %d days", n, TRASH_DAYS)
        return n

    async def _purge_one(self, kind, ref):
        c = self.conn
        if kind == "fact":
            f = self.mem.get_fact(ref)
            if f and f["status"] == "trashed":
                try:
                    vec = await self.embedder.one(f["text"])
                except Exception:
                    vec = None
                self.mem.forget(fact_ids=[ref], texts=[f["text"]], embeddings=[vec] if vec else None)
        elif kind == "entity":
            e = self.mem.get_entity(ref)
            if e and e.get("trashed_at"):
                c.execute("UPDATE facts SET entity_id = NULL WHERE entity_id = ?", (ref,))
                c.execute("DELETE FROM edges WHERE a = ? OR b = ?", (ref, ref))
                c.execute("DELETE FROM area_links WHERE kind = 'entity' AND ref_id = ?", (ref,))
                c.execute("DELETE FROM entities WHERE id = ?", (ref,))
        elif kind == "episode":
            ep = self.mem.get_episode(ref)
            if ep and ep.get("trashed_at"):
                self.mem.forget(episode_ids=[ref])
        elif kind == "reminder":
            c.execute("DELETE FROM reminders WHERE id = ? AND status = 'trashed'", (ref,))
        elif kind == "item":
            if (item := self.brain.get(ref, trashed=True)) and item["trashed_at"]:
                await self.brain.delete(ref)
        node = f"{kind}:{ref}"
        c.execute("DELETE FROM graph_links WHERE source = ? OR target = ?", (node, node))

    # helpers ---------------------------------------------------------------------------------------------------
    def _rerender_fact(self, fact_id):
        f = self.mem.get_fact(fact_id)
        if self.consolidator and f:
            if f.get("entity_id"):
                self.consolidator.render_entity(f["entity_id"])
            self.consolidator.render_inbox()

    def _render_episode_day(self, ep):
        if self.consolidator and ep:
            day = datetime.datetime.fromtimestamp(ep["start_ts"], self.brain.tz).date()
            self.consolidator.render_journal(day)
            rel = f"Journal/{day.isoformat()}.md"
            if not self.mem.episodes_between(*(lambda s: (s, s + 86400))(
                    datetime.datetime.combine(day, datetime.time(), self.brain.tz).timestamp())):
                self.vault.trash_note(rel)  # the day's only diary entry went to the trash
