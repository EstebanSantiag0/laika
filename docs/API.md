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

## Memory graph (read-only)

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

`origin` is the channel the turn belongs to (`web`, `telegram`, or `app` for reminder events).
Clients normally show everything, whatever the origin.

A delivered reminder arrives as two events: a `message` (`role: "system"`, text `"⏰ <text>"`,
plus `"\n(was due …)"` when late, `meta.reminder.id`) and a `reminder` event with
`status: "delivered"`. After done or snooze, the message is re-sent with `update: true` and
`meta.reminder.answer`.
