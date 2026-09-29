"""Rolling long-term memory: folds messages that fall out of the recent window into a summary."""

import asyncio
import logging
import time

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.llm import LLMClient, LLMError
from backseat.prompts import SUMMARY_FOCUS_HINT, SUMMARY_SHRINK, SUMMARY_SYSTEM, SUMMARY_USER, participant_lines
from backseat.render import IdMap, LineFormatter
from backseat.storage import Storage, StoredMessage, Summary

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
        bot_config: BotConfig,  # the platform the prompt names and the panel's model order
    ) -> None:
        self._storage = storage
        self._llm = llm
        self._settings = settings
        self._formatter = formatter
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
        """Fold the oldest unsummarized chunk into the summary; False if there is nothing to fold yet."""
        runtime = await self._bot_config.runtime()
        if not runtime.summary_enabled:
            return False  # switched off in the panel or .env: the bot keeps only the recent window
        summary = await self._storage.get_summary(chat_id)
        tail = await self._storage.messages_after(chat_id, summary.upto_message_id if summary else 0, _TAIL_FETCH)
        costs = [self._formatter.cost(message) for message in tail]
        # Fold as soon as something falls out of the verbatim window, so every message is always
        # either quoted or summarized. Folding the oldest chunk may overlap the window — harmless.
        if sum(costs) <= runtime.recent_context_tokens:
            return False

        chunk = self._oldest_chunk(tail, costs)
        completion = await self._llm.complete(
            self._prompt(summary, chunk),
            max_tokens=self._settings.summary_max_words * _TOKENS_PER_WORD,
            temperature=0.2,
            models=runtime.models,
            providers=runtime.providers,
        )
        text = completion.text.strip()
        if completion.finish_reason == "length":
            # Keep whole lines only: a sentence cut in half would be carried into every later summary.
            text = text.rsplit("\n", 1)[0].rstrip() if "\n" in text else text
            log.warning("chat=%s the summary hit max_tokens and was cut; lower SUMMARY_MAX_WORDS", chat_id)
        await self._storage.set_summary(chat_id, text, chunk[-1].message_id, int(time.time()))
        log.info("chat=%s summary now covers up to message %s (%d folded)", chat_id, chunk[-1].message_id, len(chunk))
        return True

    def _oldest_chunk(self, tail: list[StoredMessage], costs: list[int]) -> list[StoredMessage]:
        """The oldest messages of the tail that fit into SUMMARY_CHUNK_TOKENS, at least one."""
        chunk: list[StoredMessage] = []
        used = 0
        for message, cost in zip(tail, costs, strict=True):
            if chunk and used + cost > self._settings.summary_chunk_tokens:
                break
            chunk.append(message)
            used += cost
        return chunk

    def _prompt(self, summary: Summary | None, chunk: list[StoredMessage]) -> list[dict[str, str]]:
        settings = self._settings
        focus_hint = (
            SUMMARY_FOCUS_HINT.format(names=", ".join(settings.focus_users.values())) if settings.focus_users else ""
        )
        system = SUMMARY_SYSTEM.format(
            platform=self._bot_config.platform,
            participants=participant_lines(settings.focus_users),
            focus_hint=focus_hint,
            max_words=settings.summary_max_words,
        )
        words = len(summary.text.split()) if summary else 0
        user = SUMMARY_USER.format(
            size=f" (слов: {words})" if summary else "",
            previous=summary.text if summary else "(пока пусто)",
            # The chunk is numbered on its own: replies to messages outside it show a bare "↩".
            lines=self._formatter.lines(chunk, IdMap(message.message_id for message in chunk)),
            max_words=settings.summary_max_words,
            shrink=SUMMARY_SHRINK if words > settings.summary_max_words else "",
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
