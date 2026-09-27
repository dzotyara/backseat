"""First start in a channel: read its recent history once, then fold it into the long-term summary,
so the bot joins knowing what the chat has been talking about."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import discord

from backseat.discord.messages import stored_message
from backseat.llm import LLMError
from backseat.storage import Storage, StoredMessage
from backseat.summarizer import Summarizer

log = logging.getLogger(__name__)

BATCH_SIZE = 500


async def backfill(channel: discord.abc.Messageable, storage: Storage, me_id: int, days: int) -> int:
    """Store the last `days` days of the channel, once per channel. Returns how many messages were stored."""
    channel_id: int = channel.id  # type: ignore[attr-defined]  # every messageable guild channel has one
    key = f"backfill:{channel_id}"
    if days <= 0 or await storage.get_meta(key):
        return 0
    after = datetime.now(UTC) - timedelta(days=days)
    batch: list[StoredMessage] = []
    stored = 0
    async for message in channel.history(limit=None, after=after, oldest_first=True):
        row = stored_message(message, me_id)
        if row is None:
            continue  # system message or nothing to remember
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            await storage.add_messages(batch)
            stored += len(batch)
            batch = []
            log.info("channel=%s backfill: %d messages so far", channel_id, stored)
    if batch:
        await storage.add_messages(batch)
        stored += len(batch)
    # Only a finished backfill counts: an interrupted one starts over, and the upsert makes that harmless.
    await storage.set_meta(key, str(int(time.time())))
    log.info("channel=%s backfill done: %d messages from the last %d days", channel_id, stored, days)
    return stored


async def fold_history(summarizer: Summarizer, chat_id: int, pause: float = 1.0) -> int:
    """Fold the unsummarized history chunk by chunk until the rest fits the verbatim window.
    Returns how many chunks were folded. If every model fails, the regular after-batch
    maintenance continues from where this stopped."""
    folded = 0
    while True:
        try:
            if not await summarizer.update_once(chat_id):
                break
        except LLMError as exc:
            log.warning("chat=%s history folding stopped after %d chunks: %s", chat_id, folded, exc)
            break
        folded += 1
        log.info("chat=%s history folding: %d chunks so far", chat_id, folded)
        await asyncio.sleep(pause)
    return folded
