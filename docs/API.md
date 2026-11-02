# Laika HTTP and WebSocket API

The contract the clients use: the PWA (`app/web/`) and the Flutter app `laika-app` (Linux desktop
pet + Android). The app listens on `127.0.0.1:3010` on the server. Clients reach it over the
tailnet through `tailscale serve` (`https://<server>.<tailnet>.ts.net`). Times are ISO 8601 with
the server's offset (Europe/Madrid, e.g. `2026-11-06T17:00:00-06:00`), unless a field says
it is a Unix timestamp.

## Access

- Every path except `GET /health` requires the header `tailscale-user-login` to equal the
  configured login (`ALLOWED_TS_LOGIN` in `.env`). `tailscale serve` adds this header for requests
  from the tailnet, so a client on the tailnet needs nothing extra. Anything else gets
  `403 {"error": "forbidden"}`; the WebSocket closes with code `4403`.
- When `ALLOWED_TS_LOGIN` is empty, the check is off (development only).

## Errors

Every error is JSON with an `error` string (since SB7, 2026-11-02; FastAPI's `{"detail": …}` is not
used). Some errors add a field:
- `400 {"error"}`: a malformed request (bad JSON, a bad query parameter).
- `403 {"error": "forbidden"}`: not from the tailnet.
- `404 {"error"}`: no such thing.
- `409 {"error", "node_id"?}`: a stale edit (see `base_updated_at`), or an area delete.
- `413 {"error", "max_mb"}`: an upload over the limit.
- `415 {"error"}`: a file format she can't read.
- `422 {"error", "field"}`: a bad value for a field.

## Health

### `GET /health` (open)
```json
{"ok": true, "llm": true, "telegram": true}
```
- `ok`: the app answers.
- `llm`: llama-server's own `/health` answered 200 within 5 s. The desktop pet wakes only when
  `ok && llm`; no answer or `llm: false` means sleeping.
- `telegram`: a Telegram channel is configured.

## Chat

### `GET /api/history?before=<id>&limit=<n>`
The newest page of messages (newest last). `limit` ≤ 200, default 50. `before` pages backwards.
```json
{"messages": [Message, ...]}
```
**Message**:
| field | type | notes |
|---|---|---|
| `id` | int | |
| `ts` | float | Unix time |
| `role` | `user` \| `companion` \| `correction` \| `system` | `companion` = Laika; `correction` = the tutor's ✏️; `system` = app notices (library, language switch, reminders) |
| `channel` | `web` \| `telegram` | where it was sent or received |
| `text` | string | |
| `lang` | string \| null | `en`, `es`, `ru`, `zh`, … |
| `audio_path` | string \| null | has audio: fetch `GET /api/audio/{id}` |
| `batch_id` | int \| null | the reply turn |
| `status` | string | user messages: `received` → `read` (blue ticks); others `sent` |
| `meta` | object | see below |
| `episode_id` | int \| null | the memory episode it belongs to |

Keys that may appear in `meta`:
- `voice: true`: a voice note.
- `client_id`: echoed from the sender.
- `pinyin`: a line for Chinese.
- `corrections: [{original, better, why}]`: on `correction` messages.
- `offer: {practice_lang, answer?}`: "Switch to …?"; answer with `POST /api/offer/{id}`.
- `reminder: {id, answer?}`: a delivered reminder; answer with the reminder endpoints.
- `download`: a file.
- `note`: the app's note on a user turn.

### `GET /api/audio/{message_id}`
The audio file of a message (voice note or Laika's speech). 404 if none.

### `POST /api/voice` (multipart `file`)
Upload a voice note (≤ 20 MB). It is transcribed and enters the chat. Returns `{"message": Message}`.

### `POST /api/library` (multipart `file`)
Add a book or file to the library (≤ `library.max_upload_mb`). Returns
`{"ok": bool, "reply": string|null, "document": {"id", "title"}|null}`.

### `GET /api/library/{doc_id}/anki`
The Anki deck (`.apkg`) of a ready book.

### `POST /api/offer/{message_id}` `{"accept": true|false}`
Answers a language-switch offer. Returns `{"ok": bool}` (`false`: that offer is no longer open).

### `GET /api/settings` → Settings; `PUT /api/settings` (partial Settings) → Settings
```json
{"practice_lang": "en|ru|zh", "corrections": "on|end|off", "voice_mode": "mirror|always|never",
 "pinyin": true, "voice_speed": 1.0, "level_en": "C2", "level_ru": "A1", "level_zh": "HSK 1"}
```
`voice_speed` ∈ {1.0, 0.85, 0.7}. An invalid value gives `400 {"error": "..."}`.

### `GET /api/profile`
`{"name": "Laika"}`: the name in her persona note (empty until she has one).

## Memory graph

### `GET /api/graph?scope=active|all`
`active` (the default): active facts, all entities and episodes, and reminders that aren't done.
`all` adds superseded and archived facts and done reminders.
```json
{"generated_at": "2026-11-01T12:00:00-06:00", "scope": "active",
 "areas": [{"id": "study", "name": "Estudios", "color": "#4f8cff"}],
 "nodes": [{"id": "fact:93", "kind": "fact", "label": "He wants to build a small robot this semester.",
            "text": "He wants to build a small robot this semester.", "areas": ["projects"],
            "time_scope": "permanent", "valid_from": null, "valid_to": null, "due_at": null,
            "importance": 0.4, "created_at": "2026-10-31T12:23:32-06:00",
            "subkind": "goal", "sensitive": false}],
 "edges": [{"source": "fact:93", "target": "entity:51", "kind": "about", "weight": 1.0}]}
```
- **Node ids**: `area:<id>`, `entity:<n>`, `fact:<n>`, `episode:<n>` and `reminder:<n>`.
- **`kind`**: `area` | `entity` | `fact` | `episode` | `reminder`.
- **`label`**: at most 60 characters. **`text`**: the full text (an entity's summary, an
  episode's diary narrative).
- **`time_scope`**:
  - `permanent`;
  - `stage` (true for this stage of his life; checked again after `review_after`, from W8.3);
  - `dated` (an event or deadline; episodes are dated by their start/end);
  - `reminder`;
  - `archived` (no longer current, but kept as history).
- **`valid_from` / `valid_to`**: ISO dates or datetimes, or null. **`due_at`**: reminders only.
- **`importance`**: 0–1.
- **Extra fields**:
  - `subkind`: entity kind `person|place|thing|topic|work`, or fact category
    `identity|habit|opinion|event|goal|preference|relationship|language|worry|us|laika_self`;
  - `sensitive`: facts only (painful or private; a client may hide these);
  - `status`: reminders only.
- **Edge `kind`**:
  - `about`: fact → entity, or episode → entity;
  - `related`: entity ↔ entity co-mentions, weight normalised to the strongest link;
  - `in_episode`: fact → the episode it was learned in;
  - `in_area`: node → `area:<id>`;
  - `similar`: fact ↔ fact, embedding cosine ≥ 0.76, at most 2 per fact; the weight is the cosine.
- Every edge's `source` and `target` is a node in the same response, including the `area:<id>`
  nodes (one per configured area, `kind: "area"`, `time_scope: "permanent"`). The size is about
  100 KB for ~190 nodes.
- **Areas** come from `config.yaml` (`areas:`, editable; ids stay stable, names and colors can
  change). Facts get up to 2 (from consolidation), entities and episodes take the 2 most common
  areas of their active facts, and reminders get theirs when they are read from the chat. A node
  may have `"areas": []`.
- Facts also carry `review_after` (an ISO date, for `stage` facts only: from that day Laika asks,
  when it fits, whether it's still true).

### Graph additions (SB7/SB7b, 2026-11-02)
- **Saved items** are nodes `item:<n>` whose `kind` is the item's kind (`idea`, `for_later`, `saved`),
  `subkind` its format, plus `status`, `title`, `tags`, `url`.
- **Tags** are nodes `tag:<name>` (`kind: "tag"`, `count`).
- **Every node** has `updated_at` (null for areas and tags), `editable` (field names, see "Editing
  the graph") and `deletable`.
  - Facts also have `polarity` and `no_mention`.
  - Entities also have `aliases`.
  - Areas also have `color`.
- **Every edge** has `id`, `origin` (`computed` | `auto` | `user`) and `label`.
  - Computed edges: `id` is `<kind>:<source>:<target>`, e.g. `similar:fact:12:fact:40`.
  - Stored links: `link:<n>` (he drew it) or `about:<n>` (an item's automatic link, `label` = Laika's
    reason).
  - New edge kinds: `tagged` (item → tag), `link` (his own), and `about` from items to entities, facts
    or items.
- Hidden edges and trashed nodes never appear. Unknown `kind` values may come later: treat an unknown
  node kind as `saved`.

## Second brain: items (SB7)

**Item**:
```json
{"id": 12, "kind": "idea", "title": "Herb garden on the balcony", "text": "...", "note": "",
 "areas": ["projects"], "tags": ["garden"], "format": "text", "url": null, "doc": null,
 "status": "active", "created_at": "2026-11-02T18:00:00-06:00", "updated_at": "...",
 "resurface_at": "2026-11-05T18:00:00-06:00"}
```
- **`kind`**: `idea` | `for_later` | `saved`.
- **`format`**: `text` | `link` | `video` | `pdf` | `book` | `image`.
- **`status`**: `active` | `done` | `archived`.
- **`doc`** (when the item has a library document):
  `{"id", "title", "state", "progress": {"chunks": {done, failed, todo}, "sections": {summarized, failed, todo}, "pages"}, "pages"}`.
  - `state`: `queued` | `indexing` | `ocr_pending` | `ready` | `ready_partial` | `failed`.
  - `ready_partial`: some passages or summaries failed and were skipped.
  - `GET /api/items/{id}` alone always shows the current state (the WebSocket event is an extra).
- Every library document (sent from any channel) is also a `saved` item.

### `POST /api/items`
- **Multipart:** field `file`, plus optional fields `kind`, `areas` (comma-separated), `tags`
  (comma-separated) and `note`.
- **Or JSON:** `{"url": "https://…"}` or `{"text": "…"}`, with the same optional keys (`areas` and `tags`
  as arrays).
- **Answers:**
  - `201 {"item"}`: saved. The slow work (extraction, filing, fetching) continues in the background.
  - `200 {"item": <existing>, "duplicate": true}`: the same file (sha256) or the same URL (lowercase host,
    no `www.`, no fragment or tracking parameters, no trailing slash).
  - `201 {"item", "restored": true}`: the same file or URL was in the trash. It comes back: the same item
    with its notes, tags and links (SB8).
  - Errors: `413 {"error", "max_mb"}`, `415` for a format she can't read, `422 {"error", "field"}`.
- **`comment`** (multipart field `comment=true`, or JSON `"comment": true`): Laika also reacts to it.
  - The answer doesn't wait. In the background, once the file's text is extracted (≤2 min), a user
    message `📎 <title>` (`meta.attachment.item_id`) enters the web chat and she replies as usual (WebSocket
    `message` events).
  - Her note has the OCR text of a picture, or the summary or start of a document.
  - She can't see pictures: for one without text she asks what it is.
- **Who files it:**
  - Without `kind`, a text is filed by Laika (kind, title, areas, tags, links to existing dots). A URL
    becomes `saved`.
  - What he sends wins: Laika fills in only what's missing.

### `GET /api/items?kind=&area=&tag=&status=&q=&before=&limit=`
- Returns `{"items": [Item], "next_before": <id>|null}`.
- Sorted by id, newest first. `before` is an exclusive id cursor.
- `limit`: 30 by default, at most 100.
- `status`: `active` (the default) | `done` | `archived` | `all`.
- All filters combine with AND. `q` searches the items by meaning and by words.

### `GET /api/items/{id}` → `{"item"}`

### `PATCH /api/items/{id}`
- Body: any of `title`, `text`, `note`, `kind`, `areas` (≤3), `tags` (≤3), `status`, and
  `base_updated_at` (see "Editing the graph").
- Returns `{"item"}`. A title he sets is kept: refiling won't change it.

### `POST /api/items/{id}/resurface` `{"action": "dismiss"|"done"|"later"}`
- Returns `{"item"}`.
- `later` moves `resurface_at` to the next step (3 → 10 → 30 days).
- `dismiss` stops it coming back.
- `done` also sets `status: done`.

### `GET /api/items/{id}/file`
The original file (PDF, EPUB, HTML snapshot…), with its Content-Type and Range support. `404` if the
item has none.

### `GET /api/items/{id}/pages/{n}.png?w=1200`
- PDF page `n` (1-based) as a PNG, `w` from 200 to 2000 px wide (1200 by default).
- Cached on the server.
- Errors: `404` beyond the last page, `415` for a document that isn't a PDF.

### `GET /api/search?q=&limit=`
`{"results": [{"item_id", "doc_id", "chunk_id", "title", "kind", "format", "snippet", "text",
"page_from", "page_to", "score"}]}`.
- **Scope:** items and library passages, by meaning and by words, fused (reciprocal rank fusion).
- **`snippet`:** about 200 characters around the match.
- **`page_from`/`page_to`:** the PDF pages of a library passage (SB3); null for items and for other formats.
- **`limit`:** 20 by default.

### `GET /api/resurface` → `{"items": [Item]}`
- Ideas and "for later" items due to come back today, at most 3.
- No model call: the pet may fetch it once after a successful `/health` wake.

### `GET /api/tags` → `[{"tag", "count"}]`

## Likes and dislikes (facts with a polarity)

### `GET /api/likes` → `{"likes": [Fact], "dislikes": [Fact]}`
- Only active facts with a `polarity`, newest first. Archived and superseded facts never appear.
- A Fact is the graph's fact node (`id` `"fact:<n>"`).

### `PATCH /api/likes/{fact_id}`
- `{"polarity": "like"|"dislike"|null}`, or `{"status": "archived"}` (no longer true). Returns the Fact.
- **`fact_id` in the path is the number `<n>`, not `"fact:<n>"`.** `"fact:12"` in the path gives `422`
  or `404`.

## Editing the graph (SB7b)

Paths take the graph's string ids, URL-encoded (`fact%3A12`, `tag%3Apasaporte`).

### `PATCH /api/nodes/{node_id}`
- **Body:** fields from the node's `editable` list, plus an optional `base_updated_at`. If that doesn't
  match the node's `updated_at` (for example an Obsidian edit came in after the app loaded it):
  `409 {"error", "node_id"}`. Without it, the last write wins.
  - It is compared as an instant, to the second, so `…T18:05:10.000Z` matches `…T12:05:10-06:00`.
  - A value that isn't a date gives `422`.
- **Returns** `{"node", "edges": [every edge touching it]}`.
- **`?dry_run=true`** (tags only): `{"merges_into": tag|null, "nodes_affected": n}`.
- **Fields:**

| Kind | Field: type |
|---|---|
| item | `title` string ≤120 · `text` string · `note` string · `kind` idea\|for_later\|saved · `areas` [area id] ≤3 · `tags` [string] ≤3 · `status` active\|done\|archived |
| fact | `text` string ≤500 · `areas` [area id] ≤3 · `time_scope` permanent\|stage\|dated\|archived · `valid_from`, `valid_to`, `review_after` YYYY-MM-DD\|null · `polarity` like\|dislike\|null · `sensitive` bool · `no_mention` bool |
| entity | `name` string ≤80, unique · `aliases` [string] · `summary` string · `subkind` person\|place\|thing\|topic\|work |
| episode | `title` string ≤120 · `text` string (the diary entry) |
| reminder | `text` string ≤300 · `due_at` ISO datetime (a delivered/snoozed one goes back to pending) · `areas` [area id] ≤2 |
| area | `name` string ≤40 · `color` `#rrggbb` (areas can't be deleted) |
| tag | `name` string ≤40, no `:`. Renaming to an existing tag merges them; the merge can be undone from the trash |

### `DELETE /api/nodes/{node_id}?with_facts=true&dry_run=true`
- Moves the node to the trash for 30 days and returns `{"trashed": [node ids], "edges_removed": [edge ids]}`.
  `dry_run=true` returns the same and changes nothing. Areas give `409`.
- **What goes with it:**
  - **fact:** the fact only. After 30 days it is forgotten for good (she won't learn it again).
  - **entity:** its facts stay, but lose their link while it's in the trash. With `with_facts=true`,
    they go with it.
  - **episode:** its facts and the chat messages stay.
  - **item:** its library document goes too, unless another item shares it.
  - **reminder:** won't be delivered. This is different from done: done stays in history.
  - **tag:** removed from every item.
  - **His links** to a trashed dot go with it.
- One restore brings back everything.

### Links
| Route | Does |
|---|---|
| `POST /api/edges` `{"source", "target", "label"?}` | Creates a `link`: `201 {"edge"}`; the same link again: `200 {"edge", "duplicate": true}`. `422 {"error", "field"}` for a missing or trashed node, a self-loop, or a label over 80 characters |
| `PATCH /api/edges/{edge_id}` `{"label"}` | `link:` and `about:` edges only. Returns `{"edge"}` |
| `DELETE /api/edges/{edge_id}` | Returns `{"edges_removed": [id]}` |

What `DELETE /api/edges/{edge_id}` does depends on the link:
- **`link:`** (one he drew): goes to the trash.
- **`about:`** or a computed link: hidden. The nightly jobs never bring it back.
- **`in_area` / `tagged`:** edits the node's `areas` / `tags` instead.

### Trash
- `GET /api/trash` → `{"nodes": [node + "trashed_at", "purge_at", "with"], "hidden_edges": [edge + "hidden_at"]}`
  - A tag rename shows as `tag:<old>` with `renamed_to`.
  - Hidden edges have no purge date.
- `POST /api/trash/{node_id}/restore` → `{"restored": [node ids], "edges": [edges]}`.
- `POST /api/trash/edges/{edge_id}/restore` → `{"edge"}`: restores a link from the trash, or un-hides a
  hidden one.
- After 30 days, the nightly job deletes trashed nodes for good. Telling Laika "forget X" in the chat
  still deletes at once.

## Reminders

**Reminder**:
```json
{"id": 1, "text": "Pagar la renta", "due_at": "2026-11-06T17:00:00-06:00", "status": "pending",
 "areas": [], "source_message_id": 788, "time_defaulted": false, "created_at": "...",
 "delivered_at": null, "done_at": null, "snoozes": 0}
```
- **`status`**:
  - `pending`: waiting for its time;
  - `delivered`: sent, not marked done;
  - `snoozed`: it has a new `due_at` and will be sent again;
  - `done`.
- **`time_defaulted`**: he gave a day but no time, so the default time (10:00) was used.
- **`source_message_id`**: the chat message where he asked for it.

### `GET /api/reminders?status=pending|all`
`pending` (the default) lists everything that isn't done (`pending`, `delivered`, `snoozed`),
ordered by `due_at`. `all` includes done ones.

### `POST /api/reminders/{id}/done`
Returns the Reminder. 404 if there is no such reminder.

### `POST /api/reminders/{id}/snooze` `{"minutes": 60}`
The body is optional (default 60). `minutes` is an integer from 1 to 43200. It sets
`status: snoozed` and `due_at = now + minutes`. Returns the Reminder; 404 if it is unknown or
already done; 400 for bad minutes.

Reminders are created only from the chat ("recuérdame…", "remind me…"); there is no POST to create
one. When the server was down at a reminder's time, it is delivered at the next start, labelled
`(was due …)`; reminders are never dropped.

## WebSocket `/ws`

Every server event goes to every open socket (all tabs and devices), as JSON objects.

### Client → server
```json
{"type": "message", "text": "hola", "client_id": "optional-local-id"}
{"type": "typing", "active": true, "draft": false, "recording": false}
```
- `typing`: send it on keystrokes (`active: true`). Send `draft: true` while the box still holds
  unsent text, `recording: true` every ~2 s while recording, and `active: false` when the user
  stops. Laika never replies while the user is composing.

### Server → client
| `type` | fields | meaning |
|---|---|---|
| `message` | `message` (Message), `origin`, `voice?` (bool), `update?` (bool) | a new message; with `update: true`, an existing message changed (an offer or reminder was answered, so remove its buttons). `voice: true`: this bubble will also come as audio |
| `read` | `ids` [int], `origin` | these user messages were read (blue ticks) |
| `typing` | `origin` | Laika is writing a bubble |
| `typing_stop` | `origin` | she stopped |
| `recording` | `origin` | she is recording a voice bubble |
| `audio` | `message` (Message with `audio_path`), `origin` | the audio for a bubble is ready |
| `audio_failed` | `message`, `origin` | speech failed; the text stays |
| `file` | `message`, `filename`, `origin` | a file for him (Anki deck); fetch it via the message's `meta` |
| `settings` | `settings` (Settings), `origin` | settings changed |
| `error` | `text`, `origin` | the turn failed |
| `reminder` | `reminder` (Reminder), `origin: "app"` | a reminder was created, delivered, snoozed or done |
| `item` | `item` (Item), `origin: "app"` | a saved item was created, filed or changed (SB7; an extra: GET is the source of truth) |

`origin` is the channel the turn belongs to (`web`, `telegram`, or `app` for reminder events).
Clients normally show everything, whatever the origin.

A delivered reminder arrives as two events: a `message` (`role: "system"`, text `"⏰ <text>"`,
plus `"\n(was due …)"` when late, `meta.reminder.id`) and a `reminder` event with
`status: "delivered"`. After done or snooze, the message is re-sent with `update: true` and
`meta.reminder.answer`.
