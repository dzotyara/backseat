"""Rolling long-term memory: folds messages that fall out of the recent window into a summary."""

import asyncio
import logging
import time

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.llm import LLMClient, LLMError
from backseat.prompts import PARTICIPANT_LINE, SUMMARY_FOCUS_HINT, SUMMARY_SYSTEM
from backseat.render import IdMap, LineFormatter
from backseat.storage import Storage

log = logging.getLogger(__name__)

_TAIL_FETCH = 5000
# Russian runs 3–5 tokens a word and models overshoot the word limit: leave plenty of room,
# a summary cut off at max_tokens would lose its last sections on every fold.
_TOKENS_PER_WORD = 6


class Summarizer:
    def __init__(
        self,
        storage: Storage,
        llm: LLMClient,
        settings: CoreSettings,
        formatter: LineFormatter,
        *,
        platform: str = "Telegram",  # the summary prompt names it, like BotIdentity.platform
        bot_config: BotConfig | None = None,  # the panel's model order; None = settings only
    ) -> None:
        self._storage = storage
        self._llm = llm
        self._settings = settings
        self._formatter = formatter
        self._platform = platform
        self._bot_config = bot_config
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
        costs = [self._formatter.cost(message) for message in tail]
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
            platform=self._platform,
            participants=participants,
            focus_hint=focus_hint,
            max_words=settings.summary_max_words,
        )
        previous = summary.text if summary else "(пока пусто)"
        # The chunk is numbered on its own: replies to messages outside it show a bare "↩".
        lines = self._formatter.lines(chunk, IdMap(message.message_id for message in chunk))
        user = f"ТЕКУЩАЯ СВОДКА:\n{previous}\n\nСЛЕДУЮЩИЙ КУСОК ПЕРЕПИСКИ:\n{lines}"
        panel = {"models": (await self._bot_config.runtime()).models} if self._bot_config else {}
        completion = await self._llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=settings.summary_max_words * _TOKENS_PER_WORD,
            temperature=0.2,
            **panel,
        )
        if completion.finish_reason == "length":
            log.warning("chat=%s the summary hit max_tokens and was cut; lower SUMMARY_MAX_WORDS", chat_id)
        await self._storage.set_summary(chat_id, completion.text.strip(), chunk[-1].message_id, int(time.time()))
        log.info("chat=%s summary now covers up to message %s (%d folded)", chat_id, chunk[-1].message_id, len(chunk))
        return True
