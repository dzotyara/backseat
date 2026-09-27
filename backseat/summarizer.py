"""Rolling long-term memory: folds messages that fall out of the recent window into a summary."""

import asyncio
import logging
import time

from backseat.config import Settings
from backseat.llm import LLMClient, LLMError
from backseat.prompts import PARTICIPANT_LINE, SUMMARY_FOCUS_HINT, SUMMARY_SYSTEM
from backseat.render import LineFormatter, estimate_tokens
from backseat.storage import Storage

log = logging.getLogger(__name__)

_TAIL_FETCH = 5000
_SUMMARY_MAX_TOKENS = 3000


class Summarizer:
    def __init__(self, storage: Storage, llm: LLMClient, settings: Settings, formatter: LineFormatter) -> None:
        self._storage = storage
        self._llm = llm
        self._settings = settings
        self._formatter = formatter
        self._locks: dict[int, asyncio.Lock] = {}

    async def maintain(self, chat_id: int) -> None:
        """Fold one chunk if the unsummarized tail outgrew the recent window. Never raises."""
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            try:
                await self.update_once(chat_id)
            except LLMError as exc:
                log.warning("chat=%s summary update postponed: %s", chat_id, exc)
            except Exception:
                log.exception("chat=%s summary update failed", chat_id)

    async def update_once(self, chat_id: int) -> bool:
        settings = self._settings
        summary = await self._storage.get_summary(chat_id)
        upto = summary.upto_message_id if summary else 0
        tail = await self._storage.messages_after(chat_id, upto, _TAIL_FETCH)
        costs = [estimate_tokens(self._formatter.line(message)) for message in tail]
        # Fold as soon as something falls out of the verbatim window, so every message is always
        # either quoted or summarized. Folding the oldest chunk may overlap the window — harmless.
        if sum(costs) <= settings.recent_context_tokens:
            return False

        chunk, used = [], 0
        for message, cost in zip(tail, costs, strict=True):
            if chunk and used + cost > settings.summary_chunk_tokens:
                break
            chunk.append(message)
            used += cost

        participants = "".join(
            PARTICIPANT_LINE.format(name=name, user_id=user_id) for user_id, name in settings.focus_users.items()
        )
        focus_hint = (
            SUMMARY_FOCUS_HINT.format(names=", ".join(settings.focus_users.values())) if settings.focus_users else ""
        )
        system = SUMMARY_SYSTEM.format(
            participants=participants, focus_hint=focus_hint, max_words=settings.summary_max_words
        )
        previous = summary.text if summary else "(пока пусто)"
        user = f"ТЕКУЩАЯ СВОДКА:\n{previous}\n\nСЛЕДУЮЩИЙ КУСОК ПЕРЕПИСКИ:\n{self._formatter.lines(chunk)}"
        completion = await self._llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0.2,
        )
        await self._storage.set_summary(chat_id, completion.text.strip(), chunk[-1].message_id, int(time.time()))
        log.info("chat=%s summary now covers up to message %s (%d folded)", chat_id, chunk[-1].message_id, len(chunk))
        return True
