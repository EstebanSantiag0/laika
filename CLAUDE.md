# CLAUDE.md

Notes for Claude Code sessions working in this repository.

## What this is
Laika: a local AI language companion (FastAPI app + llama.cpp + Qwen3-TTS/Whisper + embeddings) run
with Docker Compose on one home server. The Spanish README explains the setup; `docs/architecture.md`,
`docs/memory.md` and `docs/API.md` explain the design. The Flutter client lives in
https://github.com/EstebanSantiag0/laika-app and implements `docs/API.md`: change the API only together
with that doc, and say so in the commit.

## Rules
- Never commit `.env`, `data/`, `vault/`, `models/`, database files, logs, tokens, Telegram ids or
  real personal facts. Who the user is lives in `config.yaml` `user:` (name, default "Dasha"; pronouns
  he/she/they, default they; optional gender), written by `scripts/setup.py`. Prompt templates use `{user}` /
  `{user_about}` and are written with he/him/his; `config.personal()` fills them and rewrites the pronouns
  (with verb agreement for "they") before `.format()`. Any new model-facing text must go through `personal()`.
- Prompts must stay append-only where they are replayed: the app replays past turns byte-exact
  (`render.py`) so llama.cpp keeps its prompt cache. Changing the system prompt costs one full
  re-read; run `docker compose exec app python -m laika.admin warm` after deploying such a change.
- Style rules for Laika are taught with examples plus the code guard (`slop.py`, `slop.yaml`); never
  put lists of banned phrases into the prompt.
- Background model work goes to slot 1 (`llm.background_slot`); slot 0 belongs to the conversation.
- Back up `data/laika.db` before any migration (`sqlite3` `.backup`, or the copy in
  `vault/.laika/backup/`). Schema changes are additive (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD
  COLUMN`).
- Pinned versions (see `docs/architecture.md`): upgrade one at a time and re-run the tests.

## Common commands
```bash
docker compose --profile memory up -d --build          # start / rebuild
docker compose logs -f app                             # logs
docker compose exec app python -m laika.admin warm     # after an llm restart or prompt change
docker compose exec app python -m laika.admin nightly  # force the nightly memory job
docker run --rm -v "$PWD/app:/app:ro" -e PYTHONDONTWRITEBYTECODE=1 -w /app local/laika-app:0.1 python -m tests.test_engine
```
Tests: `test_engine test_render test_memory test_consolidate test_edits test_recall test_reminders
test_api test_areas test_telegram_user` (fake model and embedder; no GPU needed).
