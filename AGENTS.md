# Backseat — notes for AI coding agents

«Botyara» v1: group-chat bots for Telegram and Discord with long-term memory — chat members, not
assistants. One platform-independent core, two chat adapters and a local web settings panel.
Python 3.14, aiogram 3.31, discord.py 2.7, FastAPI + Jinja2, SQLite (aiosqlite), OpenRouter over httpx,
pydantic-settings. User-facing docs are in README.md (Russian).

## Commands

- Tests: `.venv/Scripts/python -m pytest` (Linux/macOS: `.venv/bin/python`). No network: Telegram,
  Discord and OpenRouter are faked; `tests/test_telegram_handlers.py` drives the real aiogram Dispatcher,
  `tests/web/` uses FastAPI's TestClient, `tests/discord/` uses small fakes (no `__init__.py` there:
  a package named `tests/discord` would shadow the real `discord`).
- Lint and format: `ruff check backseat tests`, `ruff format backseat tests` (line length 120).
- Run: `python -m backseat.telegram`, `python -m backseat.discord`, `python -m backseat.web`
  (`python -m backseat` is an alias for Telegram). Never run a bot with the production token while
  production is up: two Telegram pollers steal each other's updates, and a second Discord client
  answers everything twice.

## Layout

- Core (`backseat/*.py`, no platform imports): `storage.py` (SQLite), `context.py` (prompt assembly),
  `responder.py` (batching and replies), `summarizer.py`, `digest.py`, `llm.py`, `bot_config.py`
  (persona, names, runtime overrides), `heartbeat.py`, `render.py` (transcript lines, `IdMap`),
  `replies.py` (output protocol), `triggers.py` (names, trivial chatter), `transport.py` (the
  `Transport` protocol every adapter implements), `prompts.py` (service prompts), `config.py`
  (`CoreSettings`), `commands.py` (what both bots' commands share: `/status`, reset words, a persona
  sent as a file), `logs.py` (the log format of every entry point).
- Adapters: `backseat/telegram/` and `backseat/discord/` — settings with the token, a `Transport`,
  message parsing, commands, the app wiring. `backseat/web/` — the panel: `app.py` wires it,
  `security.py` (Host, CSRF and framing guard), `pages.py` (what every page shares: `Panel`, flash
  messages, error pages), `routes/` (one module per section), `bots.py` (a bot's database as the panel
  sees it), `forms.py`.
- Each bot has its own SQLite file (`data/backseat.db`, `data/discord.db`); the panel opens both.

## How a message flows

1. The adapter stores every message (`storage.py`) and calls `Responder.enqueue()`.
2. `responder.py` batches messages per chat (debounce). A batch that addresses the bot (@mention,
   one of its names, any reply to it — even a bare sticker) is always answered: the model gets no
   veto, and a fallback phrase is sent if every model fails. Otherwise the model answers with the
   protocol `SKIP` / `REPLY #n` + text / `REACT #n emoji`, parsed by `replies.parse_action`;
   anything unparseable is a SKIP. With `PRECHECK_CONTEXT_TOKENS` > 0 a cheap yes/no precheck on the
   last few lines (no memory, same system prompt) gates that full call: in a busy Discord channel it
   was ~300 full-context calls a day. While the bot is paused (web panel) batches are dropped, but the
   messages are already stored and the summary is still maintained.
3. `context.py` builds the prompt: service rules + persona (system message), then ПАМЯТЬ ЧАТА
   (summary), ЧТО ПИСАЛИ РАНЬШЕ (older messages of `FOCUS_USERS` and of the new messages' authors),
   replied-to messages, ПОСЛЕДНЯЯ ПЕРЕПИСКА (within `RECENT_CONTEXT_TOKENS`), НОВОЕ, and the task.
   Messages are numbered per prompt with `IdMap` (#1 = oldest shown): Discord ids are 19-digit
   snowflakes, and the model must copy a number back to reply.
4. `summarizer.py` folds the oldest unsummarized chunk into the summary as soon as the tail exceeds
   the verbatim window. Each fold rewrites the whole summary, so the prompt spells out its current
   size: the model ignored the word limit, grew it to twice the size and got cut at max_tokens every
   time. The Discord adapter backfills `BACKFILL_DAYS` of history on first start (`discord/backfill.py`)
   and folds it all — for ~45k messages that is a few hundred folds.
5. `digest.py` posts the weekly digest (Sunday 20:00 Europe/Moscow by default; the `meta` table
   prevents double posts); Discord's `/digest` uses `WeeklyDigest.compose` on demand.
6. `llm.py` walks the model list in order, cools a model down after 402/403/404/429, and disables
   hidden reasoning by default (`REASONING=off`). `PROVIDERS` is sent as OpenRouter's provider order
   with fallbacks: left alone, OpenRouter served the default model from hosts 6-8x pricier than the cheapest.
   Each call logs its model, tokens, cost, provider and cached tokens.

## Web panel contract

- The bots and the panel share each bot's SQLite file (WAL; keep transactions short).
- Bot-wide settings live in `chat_settings` with chat_id 0 (`bot_config.GLOBAL`): `persona`, `names`,
  and `runtime` — JSON overrides of `Runtime` (paused, models, unprompted cooldown, reactions,
  weekly digest). `.env` values are the defaults; `BotConfig.runtime()` merges them.
- Every minute each bot writes meta `heartbeat` and republishes its defaults (`heartbeat.py`,
  `BotConfig.publish_defaults`); `chat_title:<id>` names the chats for the panel.
- Access is only through an SSH tunnel: compose publishes `127.0.0.1:8090` on the server. The panel
  has no login, so it rejects foreign Host headers and cross-site POSTs and never shows secrets.

## Conventions and gotchas

- Prompts and everything users see are Russian; code, comments and logs are English.
- Identify people by user id, never by display name (`FOCUS_USERS=id:name`).
- Telegram entity offsets are UTF-16: use `MessageEntity.extract_from()`, never slice the text.
- aiogram 3.31: `Message.edit_date` is an int (unix seconds), while `Message.date` is a datetime.
- Telegram menu commands must be Latin; Cyrillic aliases (`/промпт`, `/имена`) work through
  `Command(..., ignore_case=True)`, which also reads captions.
- Discord: bot text must never ping anyone — the client default is `AllowedMentions.none()`, and the
  transport only lets a reply ping its author. Edits arrive through `on_raw_message_edit`.
  Discord wants the heart reaction with the variation selector ("❤️").
- The bot's own messages are stored with `is_bot=1` and shown to the model as «Ты».
- Keep `prompts.UNPROMPTED_TASK` and `replies.parse_action` in sync.
- Personas: `prompts/persona.md` (Telegram) and `prompts/discord.md`; `/prompt` and the panel
  override them bot-wide. `/prompt` and `/status` are owner-only: in Telegram only in the private
  chat, in Discord hidden from non-admins and answered ephemerally — the persona says who gets
  roasted and must never be shown in the chat.
- This repository is public: no secrets, server addresses, real user or chat ids in committed files.
  Real values belong in the server's `.env` and `.env.discord`.

## Production

- The bots run on the owner's VPS from a git clone in `/root/backseat`; ask the owner for access.
- Compose there is the snap binary: `docker-compose` (with a hyphen), not `docker compose`.
  `COMPOSE_PROFILES=discord,web` in `.env` switches on the Discord bot and the panel; `.env.discord`
  is layered over `.env` for the Discord service (an empty value there means the built-in default).
- Deploy: `cd /root/backseat && git pull && docker-compose up -d --build`, then check
  `docker logs --tail 30 backseat` / `backseat-discord` / `backseat-web`.
- Memory is `data/*.db`; back it up (sqlite3 `.backup`) before any schema change.
- Other containers on that server are unrelated; leave them alone.
