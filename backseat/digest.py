"""Weekly "Итоги недели" post, by default on Sundays at 20:00 Moscow time."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backseat.config import Settings
from backseat.context import ContextBuilder
from backseat.llm import LLMClient, LLMError
from backseat.replies import clean_reply
from backseat.responder import Responder
from backseat.storage import Storage

log = logging.getLogger(__name__)

# If the bot was down at posting time, it still posts when it comes back within this window.
CATCH_UP = timedelta(hours=6)


class WeeklyDigest:
    def __init__(
        self,
        *,
        storage: Storage,
        llm: LLMClient,
        context: ContextBuilder,
        responder: Responder,
        settings: Settings,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._storage = storage
        self._llm = llm
        self._context = context
        self._responder = responder
        self._settings = settings
        self._tz = ZoneInfo(settings.timezone)
        self._now = now or (lambda: datetime.now(self._tz))

    def next_run(self, moment: datetime) -> datetime:
        """The first posting time strictly after `moment`."""
        local = moment.astimezone(self._tz)
        days_ahead = (self._settings.digest_weekday - local.weekday()) % 7
        candidate = datetime.combine(local.date() + timedelta(days=days_ahead), self._settings.digest_time, self._tz)
        if candidate <= local:
            candidate += timedelta(days=7)
        return candidate

    async def run_forever(self) -> None:
        now = self._now()
        missed = self.next_run(now) - timedelta(days=7)
        if now - missed <= CATCH_UP:
            await self._post_safely(missed)
        while True:
            now = self._now()
            run_at = self.next_run(now)
            await asyncio.sleep((run_at - now).total_seconds())
            await self._post_safely(run_at)

    async def _post_safely(self, run_at: datetime) -> None:
        try:
            await self.post_all(run_at)
        except Exception:
            log.exception("Weekly digest for %s failed", run_at)

    async def post_all(self, run_at: datetime) -> None:
        settings = self._settings
        since = int((run_at - timedelta(days=7)).timestamp())
        week = f"{run_at:%G-W%V}"
        for chat_id in await self._storage.active_group_chats(since):
            if settings.allowed_chat_ids and chat_id not in settings.allowed_chat_ids:
                continue
            key = f"digest:{chat_id}:{week}"
            if await self._storage.get_meta(key):
                continue
            if await self._storage.count_messages(chat_id, since) < settings.digest_min_messages:
                continue
            try:
                prompt = await self._context.for_digest(chat_id, since)
                completion = await self._llm.complete(prompt, max_tokens=1500)
            except LLMError as exc:
                log.warning("chat=%s weekly digest failed: %s", chat_id, exc)
                continue
            text = clean_reply(completion.text)
            if text and await self._responder.send(chat_id, text):
                await self._storage.set_meta(key, "sent")
                log.info("chat=%s weekly digest posted for %s", chat_id, week)
