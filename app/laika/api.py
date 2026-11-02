"""Read-only API for the Flutter client `laika-app` (W8.2, docs/API.md): her memory as a graph, and
reminders. The same tailnet check as the rest of the app applies (main.py middleware); only
/health is open.

The graph's nodes are life areas, entities (people, topics, works, places, things), facts, episodes
and reminders. The edges:
- about: a fact → its entity, or an episode → an entity it mentions;
- related: entity ↔ entity, from co-mentions;
- in_episode: a fact → the episode it was learned in;
- in_area: any node → a life area (W8.3);
- similar: fact ↔ fact, close in meaning (embeddings).
Weights are 0–1.
"""
import datetime
import json
import zoneinfo

import sqlite_vec
from fastapi import APIRouter, HTTPException, Query

SIMILAR_COS = 0.76   # fact ↔ fact "similar" edges (live store 2026-11-01: nearest-neighbour median 0.74;
                     # 0.79+ were real pairs, ~0.75 only loosely related)
SIMILAR_PER_FACT = 2
LABEL_CHARS = 60


def _iso(ts, tz):
    return datetime.datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds") if ts else None


def _label(text, n=LABEL_CHARS):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


class Api:
    def __init__(self, cfg, db, memory, reminders, areas=None, brain=None, edit=None):
        self.cfg, self.db, self.mem, self.reminders, self.areas = cfg, db, memory, reminders, areas
        self.brain, self.edit = brain, edit  # SB7/SB7b: saved items and graph editing
        self.tz = zoneinfo.ZoneInfo(cfg["memory"]["tz"])
        self.router = APIRouter()
        r = self.router
        r.add_api_route("/api/graph", self.graph, methods=["GET"])
        r.add_api_route("/api/reminders", self.list_reminders, methods=["GET"])
        r.add_api_route("/api/reminders/{reminder_id}/done", self.done, methods=["POST"])
        r.add_api_route("/api/reminders/{reminder_id}/snooze", self.snooze, methods=["POST"])

    # reminders ---------------------------------------------------------------------------------------
    async def list_reminders(self, status: str = Query("pending", pattern="^(pending|all)$")):
        return [self.reminders.public(r) for r in self.reminders.list(status)]

    async def done(self, reminder_id: int):
        r = await self.reminders.done(reminder_id)
        if r is None:
            raise HTTPException(404, "no such reminder")
        return self.reminders.public(r)

    async def snooze(self, reminder_id: int, body: dict | None = None):
        minutes = (body or {}).get("minutes", self.reminders.snooze_min)
        if not isinstance(minutes, int) or not 1 <= minutes <= 60 * 24 * 30:
            raise HTTPException(400, "minutes must be an integer from 1 to 43200")
        r = await self.reminders.snooze(reminder_id, minutes)
        if r is None:
            raise HTTPException(404, "no such reminder, or it is already done")
        return self.reminders.public(r)

    # graph -------------------------------------------------------------------------------------------
    async def graph(self, scope: str = Query("active", pattern="^(active|all)$")):
        return self.build_graph(scope)

    def _area_ids(self, kind, ref_id):
        return self.areas.of(kind, ref_id) if self.areas else []

    def build_graph(self, scope="active"):
        nodes, edges = [], []
        areas = self.areas.list() if self.areas else []
        known_areas = {a["id"] for a in areas}
        for a in areas:
            nodes.append(self.area_node(a))

        def link_areas(node):
            for area in node["areas"]:
                if area in known_areas:
                    edges.append({"source": node["id"], "target": f"area:{area}", "kind": "in_area", "weight": 1.0})

        ents = self.mem.entities()
        ent_ids = set()
        for e in ents:
            n = self.entity_node(e)
            nodes.append(n)
            ent_ids.add(e["id"])
            link_areas(n)
        top = max([r["weight"] for r in self.db.conn.execute("SELECT weight FROM edges")] or [1.0]) or 1.0
        for r in self.db.conn.execute("SELECT a, b, weight FROM edges"):
            if r["a"] in ent_ids and r["b"] in ent_ids:
                edges.append({"source": f"entity:{r['a']}", "target": f"entity:{r['b']}", "kind": "related",
                              "weight": round(min(1.0, r["weight"] / top), 3)})

        episodes = self.db.conn.execute("SELECT * FROM episodes WHERE trashed_at IS NULL ORDER BY id").fetchall()
        ep_ids = set()
        for ep in episodes:
            n = self.episode_node(dict(ep))
            nodes.append(n)
            ep_ids.add(ep["id"])
            link_areas(n)
            for eid in json.loads(ep["entity_ids"] or "[]"):
                if eid in ent_ids:
                    edges.append({"source": n["id"], "target": f"entity:{eid}", "kind": "about", "weight": 0.5})

        statuses = ("active",) if scope == "active" else ("active", "superseded", "archived")
        facts = self.mem.facts(statuses=statuses)
        fact_ids = {f["id"] for f in facts}
        for f in facts:
            n = self.fact_node(f)
            nodes.append(n)
            link_areas(n)
            if f["entity_id"] in ent_ids:
                edges.append({"source": n["id"], "target": f"entity:{f['entity_id']}", "kind": "about", "weight": 1.0})
            if f["episode_id"] in ep_ids:
                edges.append({"source": n["id"], "target": f"episode:{f['episode_id']}", "kind": "in_episode",
                              "weight": 0.5})
        edges += self._similar(fact_ids)

        for r in self.reminders.list("pending" if scope == "active" else "all"):
            n = self.reminder_node(r)
            nodes.append(n)
            link_areas(n)

        if self.brain:  # SB1/SB7: saved items, their tags, their links and the links he drew
            tags = {}
            for item in self.brain.items(status="active" if scope == "active" else "all"):
                n = self.item_node(item)
                nodes.append(n)
                link_areas(n)
                for t in item["tags"]:
                    tags[t] = tags.get(t, 0) + 1
                    edges.append({"source": n["id"], "target": f"tag:{t}", "kind": "tagged", "weight": 0.5})
            for t, count in sorted(tags.items()):
                nodes.append(self.tag_node(t, count))
        present = {n["id"] for n in nodes}
        hidden = set()
        stored = []
        for r in self.db.conn.execute("SELECT * FROM graph_links WHERE trashed_at IS NULL") if self.brain else []:
            if r["action"] == "hide":
                hidden |= {(r["kind"], r["source"], r["target"]), (r["kind"], r["target"], r["source"])}
            elif r["source"] in present and r["target"] in present:
                stored.append({"id": f"{r['kind']}:{r['id']}", "source": r["source"], "target": r["target"],
                               "kind": r["kind"], "weight": 1.0, "origin": "user" if r["origin"] == "user" else "auto",
                               "label": r["label"]})
        out = []
        for e in edges:
            if (e["kind"], e["source"], e["target"]) in hidden or e["target"] not in present:
                continue
            out.append({"id": f"{e['kind']}:{e['source']}:{e['target']}", **e, "origin": "computed", "label": ""})
        return {"generated_at": datetime.datetime.now(self.tz).isoformat(timespec="seconds"), "scope": scope,
                "areas": areas, "nodes": nodes, "edges": out + stored}

    # one node in the graph's format (also for PATCH answers and the trash) --------------------------------
    def node(self, node_id, trashed=False):
        kind, _, ref = node_id.partition(":")
        if not self.edit:
            return None
        row = self.edit.row(node_id, trashed=trashed)
        if row is None:
            return None
        if kind == "area":
            return self.area_node(row)
        if kind == "tag":
            return self.tag_node(ref, len(self.edit.tag_items(ref)))
        return getattr(self, f"{kind}_node")(row)

    def _meta(self, node_id, kind):
        from .graph_edit import EDITABLE
        updated = self.edit.updated_ts(node_id) if self.edit and kind not in ("area", "tag") else None
        return {"updated_at": _iso(updated, self.tz), "editable": EDITABLE.get(kind, []), "deletable": kind != "area"}

    def area_node(self, a):
        return {**self._node(f"area:{a['id']}", "area", a["name"], a["name"], [a["id"]], "permanent"),
                "color": a.get("color"), "updated_at": None, "editable": ["name", "color"], "deletable": False}

    def entity_node(self, e):
        return {**self._node(f"entity:{e['id']}", "entity", e["name"], e["summary"] or e["name"],
                             self._area_ids("entity", e["id"]), "permanent", importance=e["importance"],
                             created=e["created"], subkind=e["kind"], aliases=e["aliases"]),
                **self._meta(f"entity:{e['id']}", "entity")}

    def episode_node(self, ep):
        return {**self._node(f"episode:{ep['id']}", "episode", ep["title"], ep["narrative"],
                             self._area_ids("episode", ep["id"]), "dated",
                             importance=max(ep["arousal"], ep["self_rel"], ep["future_rel"]), created=ep["end_ts"],
                             valid_from=_iso(ep["start_ts"], self.tz), valid_to=_iso(ep["end_ts"], self.tz),
                             title=ep["title"]),
                **self._meta(f"episode:{ep['id']}", "episode")}

    def fact_node(self, f):
        return {**self._node(f"fact:{f['id']}", "fact", _label(f["text"]), f["text"], self._area_ids("fact", f["id"]),
                             self._time_scope(f), importance=f["importance"], created=f["recorded_at"],
                             valid_from=f["valid_from"], valid_to=f["valid_to"], subkind=f["category"],
                             sensitive=bool(f["sensitive"]), review_after=f.get("review_after"),
                             polarity=f.get("polarity"), no_mention=bool(f.get("no_mention"))),
                **self._meta(f"fact:{f['id']}", "fact")}

    def reminder_node(self, r):
        return {**self._node(f"reminder:{r['id']}", "reminder", _label(r["text"]), r["text"], r["areas"], "reminder",
                             created=r["created"], due_at=_iso(r["due_at"], self.tz), status=r["status"]),
                **self._meta(f"reminder:{r['id']}", "reminder")}

    def item_node(self, item):
        return {**self._node(f"item:{item['id']}", item["kind"], _label(item["title"]), item["text"] or item["title"],
                             item["areas"], "permanent", importance=0.5, created=item["created"], subkind=item["format"],
                             status=item["status"], title=item["title"], tags=item["tags"], url=item["url"]),
                **self._meta(f"item:{item['id']}", "item")}

    def tag_node(self, tag, count):
        return {**self._node(f"tag:{tag}", "tag", tag, tag, [], "permanent", importance=min(1.0, count / 10)),
                "count": count, "updated_at": None, "editable": ["name"], "deletable": True}

    def _time_scope(self, f):
        if f["status"] != "active":
            return "archived"
        return f.get("time_scope") or "permanent"

    def _node(self, nid, kind, label, text, areas, time_scope, importance=0.0, created=None, valid_from=None,
              valid_to=None, due_at=None, **extra):
        return {"id": nid, "kind": kind, "label": label, "text": text, "areas": list(areas), "time_scope": time_scope,
                "valid_from": valid_from, "valid_to": valid_to, "due_at": due_at,
                "importance": round(float(importance or 0), 3), "created_at": _iso(created, self.tz), **extra}

    def _similar(self, fact_ids):
        """Each fact's nearest other facts above SIMILAR_COS (at most 2 each), as undirected edges."""
        out, seen = [], set()
        rows = self.db.conn.execute("SELECT i.ref_id, v.embedding FROM items i JOIN items_vec v ON v.rowid = i.id "
                                    "WHERE i.kind = 'fact'").fetchall()
        for row in rows:
            if row["ref_id"] not in fact_ids:
                continue
            vec = sqlite_vec.deserialize_float32(row["embedding"]) if hasattr(sqlite_vec, "deserialize_float32") \
                else _unpack(row["embedding"])
            n = 0
            for kind, ref, cos in self.mem.search_vec(list(vec), k=SIMILAR_PER_FACT + 1, kinds=("fact",)):
                if ref == row["ref_id"] or ref not in fact_ids or cos < SIMILAR_COS:
                    continue
                key = tuple(sorted((row["ref_id"], ref)))
                if key not in seen:
                    seen.add(key)
                    out.append({"source": f"fact:{key[0]}", "target": f"fact:{key[1]}", "kind": "similar",
                                "weight": round(cos, 3)})
                n += 1
                if n >= SIMILAR_PER_FACT:
                    break
        return out


def _unpack(blob):
    import struct
    return list(struct.unpack(f"{len(blob) // 4}f", blob))
