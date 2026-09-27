# Backseat — notes for AI coding agents

Telegram group-chat bot «ботяра» v2.0: a chat member with long-term memory, not an assistant.
Python 3.14, aiogram 3.31, SQLite (aiosqlite), OpenRouter over httpx, pydantic-settings.
User-facing docs are in README.md (Russian).

## Commands

- Tests: `.venv/Scripts/python -m pytest` (Linux/macOS: `.venv/bin/python`). No network access:
  Telegram and OpenRouter are faked; `tests/test_handlers.py` drives the real aiogram Dispatcher.
- Lint and format: `ruff check backseat tests`, `ruff format backseat tests` (line length 120).
- Run: `python -m backseat` with a filled `.env`. Never run it with the production bot token while
  production is up: two pollers on one token steal each other's updates.

## How a message flows

1. `handlers.py` stores every group message (`storage.py`, SQLite) and calls `Responder.enqueue()`.
2. `responder.py` batches messages per chat (debounce). A batch that addresses the bot (@mention,
   one of its names, any reply to it — even a bare sticker) is always answered — the model gets no veto, and a
   fallback phrase is sent if every model fails. Otherwise the model answers with the protocol
   `SKIP` / `REPLY #id` + text / `REACT #id emoji`, parsed by `replies.parse_action`; anything
   unparseable is a SKIP.
3. `context.py` builds the prompt: service rules + persona (system message), then ПАМЯТЬ ЧАТА
   (summary), ЧТО ПИСАЛИ РАНЬШЕ (older messages of `FOCUS_USERS` and of the new messages' authors),
   replied-to messages, ПОСЛЕДНЯЯ ПЕРЕПИСКА (within `RECENT_CONTEXT_TOKENS`), НОВОЕ, and the task.
4. `summarizer.py` runs after each batch and folds the oldest unsummarized chunk into the summary
   once the unsummarized tail exceeds the verbatim window.
5. `digest.py` posts the weekly digest (Sunday 20:00 Europe/Moscow by default); the `meta` table
   prevents double posts.
6. `llm.py` walks `MODELS` in order, cools a model down after 402/403/404/429, and disables hidden
   reasoning by default (`REASONING=off`).

## Conventions and gotchas

- Prompts and everything users see are Russian; code, comments and logs are English.
- Identify people by Telegram user_id, never by display name (`FOCUS_USERS=id:name`).
- Telegram entity offsets are UTF-16: use `MessageEntity.extract_from()`, never slice the text.
- aiogram 3.31: `Message.edit_date` is an int (unix seconds), while `Message.date` is a datetime.
- Telegram menu commands must be Latin; Cyrillic aliases (`/промпт`, `/имена`) work through
  `Command(..., ignore_case=True)`, which also reads captions.
- The bot's own messages are stored with `is_bot=1` and shown to the model as «Ты».
- Keep `prompts.UNPROMPTED_TASK` and `replies.parse_action` in sync.
- The persona lives in `prompts/persona.md`; `/prompt` overrides it bot-wide (`chat_settings` row with
  chat_id 0, see `bot_config.py`). `/prompt` works only for owners and only in private: the persona names
  the roast target, so it must never be shown in the group. Everyone else gets silence, and the command
  is absent from their menu (`handlers.register_commands`).
- This repository is public: no secrets, server addresses or real Telegram ids in committed files.
  Real values belong in the server's `.env`.

## Production

- The bot runs on the owner's VPS from a git clone in `/root/backseat`; ask the owner for access.
- Compose there is the snap binary: `docker-compose` (with a hyphen), not `docker compose`.
- Deploy: `cd /root/backseat && git pull && docker-compose up -d --build`, then check
  `docker logs --tail 30 backseat` for the "Backseat v… started" line.
- Memory is `/root/backseat/data/backseat.db`; back it up before any schema change.
- Other containers on that server are unrelated; leave them alone.
