"""SQLite storage: every chat message, the rolling summary and per-chat settings."""

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    author     TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    reply_to   INTEGER,
    is_bot     INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    edited_at  INTEGER,
    PRIMARY KEY (chat_id, message_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS messages_by_user ON messages (chat_id, user_id, message_id);
CREATE INDEX IF NOT EXISTS messages_by_time ON messages (chat_id, created_at);

CREATE TABLE IF NOT EXISTS summaries (
    chat_id         INTEGER PRIMARY KEY,
    text            TEXT    NOT NULL,
    upto_message_id INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER NOT NULL,
    key     TEXT    NOT NULL,
    value   TEXT    NOT NULL,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COLUMNS = "chat_id, message_id, user_id, author, text, reply_to, is_bot, created_at"


@dataclass(frozen=True, slots=True)
class StoredMessage:
    chat_id: int
    message_id: int
    user_id: int
    author: str
    text: str
    reply_to: int | None
    is_bot: bool
    created_at: int  # unix seconds


@dataclass(frozen=True, slots=True)
class Summary:
    text: str
    upto_message_id: int
    updated_at: int


def _row_to_message(row: aiosqlite.Row) -> StoredMessage:
    return StoredMessage(
        chat_id=row[0],
        message_id=row[1],
        user_id=row[2],
        author=row[3],
        text=row[4],
        reply_to=row[5],
        is_bot=bool(row[6]),
        created_at=row[7],
    )


class Storage:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() was not called")
        return self._db

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # --- messages ---

    async def add_message(self, message: StoredMessage) -> None:
        await self.db.execute(
            f"INSERT INTO messages ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (chat_id, message_id) DO UPDATE SET text = excluded.text",
            (
                message.chat_id,
                message.message_id,
                message.user_id,
                message.author,
                message.text,
                message.reply_to,
                int(message.is_bot),
                message.created_at,
            ),
        )
        await self.db.commit()

    async def add_messages(self, messages: list[StoredMessage]) -> None:
        """add_message for many rows in one transaction, e.g. a channel's history."""
        await self.db.executemany(
            f"INSERT INTO messages ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (chat_id, message_id) DO UPDATE SET text = excluded.text",
            [
                (
                    message.chat_id,
                    message.message_id,
                    message.user_id,
                    message.author,
                    message.text,
                    message.reply_to,
                    int(message.is_bot),
                    message.created_at,
                )
                for message in messages
            ],
        )
        await self.db.commit()

    async def edit_message(self, chat_id: int, message_id: int, text: str, edited_at: int) -> None:
        await self.db.execute(
            "UPDATE messages SET text = ?, edited_at = ? WHERE chat_id = ? AND message_id = ?",
            (text, edited_at, chat_id, message_id),
        )
        await self.db.commit()

    async def get_message(self, chat_id: int, message_id: int) -> StoredMessage | None:
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_message(row) if row else None

    async def get_messages(self, chat_id: int, message_ids: list[int]) -> list[StoredMessage]:
        """Messages with the given ids that exist, oldest first."""
        if not message_ids:
            return []
        placeholders = ",".join("?" * len(message_ids))
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND message_id IN ({placeholders}) ORDER BY message_id",
            (chat_id, *message_ids),
        ) as cursor:
            return [_row_to_message(row) for row in await cursor.fetchall()]

    async def messages_before(self, chat_id: int, before_id: int, limit: int) -> list[StoredMessage]:
        """The `limit` newest messages with id < before_id, oldest first."""
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND message_id < ? ORDER BY message_id DESC LIMIT ?",
            (chat_id, before_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_message(row) for row in reversed(rows)]

    async def user_messages_before(self, chat_id: int, user_id: int, before_id: int, limit: int) -> list[StoredMessage]:
        """One participant's `limit` newest messages with id < before_id, oldest first."""
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND user_id = ? AND message_id < ? "
            "AND is_bot = 0 ORDER BY message_id DESC LIMIT ?",
            (chat_id, user_id, before_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_message(row) for row in reversed(rows)]

    async def messages_after(self, chat_id: int, after_id: int, limit: int) -> list[StoredMessage]:
        """The `limit` oldest messages with id > after_id, oldest first."""
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND message_id > ? ORDER BY message_id LIMIT ?",
            (chat_id, after_id, limit),
        ) as cursor:
            return [_row_to_message(row) for row in await cursor.fetchall()]

    async def messages_since(self, chat_id: int, since_ts: int, limit: int) -> list[StoredMessage]:
        """The `limit` newest messages created at or after since_ts, oldest first."""
        async with self.db.execute(
            f"SELECT {_COLUMNS} FROM messages WHERE chat_id = ? AND created_at >= ? ORDER BY message_id DESC LIMIT ?",
            (chat_id, since_ts, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_message(row) for row in reversed(rows)]

    async def count_messages(self, chat_id: int, since_ts: int = 0) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND created_at >= ?",
            (chat_id, since_ts),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def active_chats(self, since_ts: int) -> list[int]:
        """Chats that had messages at or after since_ts. Each bot has its own database,
        so these are exactly the chats this bot lives in."""
        async with self.db.execute(
            "SELECT DISTINCT chat_id FROM messages WHERE created_at >= ?",
            (since_ts,),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    # --- summary ---

    async def get_summary(self, chat_id: int) -> Summary | None:
        async with self.db.execute(
            "SELECT text, upto_message_id, updated_at FROM summaries WHERE chat_id = ?", (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return Summary(text=row[0], upto_message_id=row[1], updated_at=row[2]) if row else None

    async def set_summary(self, chat_id: int, text: str, upto_message_id: int, updated_at: int) -> None:
        await self.db.execute(
            "INSERT INTO summaries (chat_id, text, upto_message_id, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET text = excluded.text, "
            "upto_message_id = excluded.upto_message_id, updated_at = excluded.updated_at",
            (chat_id, text, upto_message_id, updated_at),
        )
        await self.db.commit()

    # --- per-chat settings (persona, names) ---

    async def get_setting(self, chat_id: int, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key)
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_setting(self, chat_id: int, key: str, value: str | None) -> None:
        if value is None:
            await self.db.execute("DELETE FROM chat_settings WHERE chat_id = ? AND key = ?", (chat_id, key))
        else:
            await self.db.execute(
                "INSERT INTO chat_settings (chat_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (chat_id, key) DO UPDATE SET value = excluded.value",
                (chat_id, key, value),
            )
        await self.db.commit()

    # --- global key/value (e.g. which week's digest was already posted) ---

    async def get_meta(self, key: str) -> str | None:
        async with self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()
