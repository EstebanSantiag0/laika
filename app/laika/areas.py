"""Life areas (W8.3, docs/PLAN-w8.md): study, projects, finance, legal, skills, work, languages, health,
people, home… The list lives in config.yaml (`areas:`), so it is easy to change; it is synced into the
`areas` table at start-up. An area removed from the config is only hidden (its links stay).

Facts, entities and episodes link to areas many-to-many in `area_links` (kind, ref_id). Consolidation
assigns areas to new facts; entities and episodes take the areas of their facts. Reminders keep their
areas in their own `areas` column (set when the reminder is read from the chat).
"""
import collections
import json

SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#888888',
    position INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS area_links (
    area TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    PRIMARY KEY (area, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS area_links_ref ON area_links(kind, ref_id);
"""
DEFAULT = [
    {"id": "study", "name": "Estudios", "color": "#4f8cff"},
    {"id": "projects", "name": "Proyectos", "color": "#9b59b6"},
    {"id": "finance", "name": "Finanzas", "color": "#2ecc71"},
    {"id": "legal", "name": "Legal/trámites", "color": "#7f8c8d"},
    {"id": "skills", "name": "Habilidades", "color": "#e67e22"},
    {"id": "work", "name": "Trabajo/prácticas", "color": "#34495e"},
    {"id": "languages", "name": "Idiomas", "color": "#e74c3c"},
    {"id": "health", "name": "Salud", "color": "#1abc9c"},
    {"id": "people", "name": "Personas", "color": "#f1c40f"},
    {"id": "home", "name": "Hogar/día a día", "color": "#a0522d"},
]
MAX_PER_ITEM = 2  # an entity or episode shows its two main areas


class Areas:
    def __init__(self, cfg, db):
        self.db = db
        self.conn = db.conn
        self.conn.executescript(SCHEMA)
        self.sync(cfg.get("areas") or DEFAULT)

    def sync(self, areas):
        """Make the table match the config: upsert listed areas in order, hide the others."""
        ids = []
        for pos, a in enumerate(areas):
            aid = str(a["id"]).strip()
            if not aid:
                continue
            ids.append(aid)
            self.conn.execute(
                "INSERT INTO areas(id, name, color, position, active) VALUES (?, ?, ?, ?, 1) ON CONFLICT(id) DO UPDATE "
                "SET name = excluded.name, color = excluded.color, position = excluded.position, active = 1",
                (aid, a.get("name") or aid, a.get("color") or "#888888", pos))
        self.conn.execute(f"UPDATE areas SET active = 0 WHERE id NOT IN ({','.join('?' * len(ids))})", ids)
        self.apply_overrides()

    def apply_overrides(self):
        """SB7b: names and colors he changed in the app win over config.yaml (kept in settings)."""
        row = self.conn.execute("SELECT value FROM settings WHERE key = 'area_overrides'").fetchone() \
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'settings'").fetchone() else None
        for aid, o in (json.loads(row[0]) if row else {}).items():
            if o.get("name"):
                self.conn.execute("UPDATE areas SET name = ? WHERE id = ?", (o["name"], aid))
            if o.get("color"):
                self.conn.execute("UPDATE areas SET color = ? WHERE id = ?", (o["color"], aid))

    def list(self):
        return [{"id": r["id"], "name": r["name"], "color": r["color"]} for r in self.conn.execute(
            "SELECT * FROM areas WHERE active = 1 ORDER BY position")]

    def ids(self):
        return [a["id"] for a in self.list()]

    def prompt_list(self):
        """'study (Estudios), projects (Proyectos), …' for the model."""
        return ", ".join(f"{a['id']} ({a['name']})" for a in self.list())

    def clean(self, area_ids):
        """Known area ids only; also accepts a name ("Idiomas") or "id (name)" from the model."""
        lookup = {}
        for a in self.list():
            lookup[a["id"].lower()] = a["id"]
            lookup[a["name"].lower()] = a["id"]
        out = []
        for a in area_ids or []:
            a = str(a).strip().lower()
            aid = lookup.get(a) or lookup.get(a.split("(")[0].strip())
            if aid and aid not in out:
                out.append(aid)
        return out[:3]

    def schema(self, max_items=2):
        """JSON schema for a list of area ids, so the model can only answer with known ids."""
        return {"type": "array", "maxItems": max_items, "items": {"enum": self.ids()}}

    def of(self, kind, ref_id):
        if kind == "reminder":
            row = self.conn.execute("SELECT areas FROM reminders WHERE id = ?", (ref_id,)).fetchone()
            return json.loads(row[0]) if row else []
        return [r[0] for r in self.conn.execute(
            "SELECT l.area FROM area_links l JOIN areas a ON a.id = l.area WHERE l.kind = ? AND l.ref_id = ? "
            "ORDER BY a.position", (kind, ref_id))]

    def set(self, kind, ref_id, area_ids):
        self.conn.execute("DELETE FROM area_links WHERE kind = ? AND ref_id = ?", (kind, ref_id))
        for a in self.clean(area_ids):
            self.conn.execute("INSERT OR IGNORE INTO area_links(area, kind, ref_id) VALUES (?, ?, ?)", (a, kind, ref_id))

    def derive(self):
        """Entities and episodes take the most common areas of their active facts."""
        rows = self.conn.execute("SELECT f.id, f.entity_id, f.episode_id, l.area FROM facts f "
                                 "JOIN area_links l ON l.kind = 'fact' AND l.ref_id = f.id "
                                 "WHERE f.status = 'active'").fetchall()
        by = {"entity": collections.defaultdict(collections.Counter),
              "episode": collections.defaultdict(collections.Counter)}
        for r in rows:
            if r["entity_id"] is not None:
                by["entity"][r["entity_id"]][r["area"]] += 1
            if r["episode_id"] is not None:
                by["episode"][r["episode_id"]][r["area"]] += 1
        for kind, counts in by.items():
            self.conn.execute("DELETE FROM area_links WHERE kind = ?", (kind,))
            for ref_id, c in counts.items():
                self.set(kind, ref_id, [a for a, _ in c.most_common(MAX_PER_ITEM)])
        return sum(len(c) for c in by.values())
