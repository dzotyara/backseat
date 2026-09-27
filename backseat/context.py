"""Assembles what the model sees: persona + memory + personal history + recent chat + the new messages."""

from collections.abc import Callable
from dataclasses import dataclass

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.prompts import DIGEST_TASK, PRECHECK_TASK, SYSTEM_TEMPLATE, participant_lines
from backseat.render import IdMap, LineFormatter
from backseat.storage import Storage, StoredMessage
from backseat.triggers import BotIdentity

ChatMessages = list[dict[str, str]]

# How many rows to read before trimming to the token budgets.
_RECENT_FETCH = 600
_HISTORY_FETCH = 300
_PRECHECK_FETCH = 200
_DIGEST_FETCH = 3000
_DIGEST_TOKENS = 20000
_RECENT = "ПОСЛЕДНЯЯ ПЕРЕПИСКА"
_NEW = "НОВОЕ"
_TIMELINE = (_RECENT, _NEW)


@dataclass(frozen=True, slots=True)
class Prompt:
    messages: ChatMessages
    ids: IdMap  # message numbers in the prompt -> real message ids


class ContextBuilder:
    def __init__(
        self,
        storage: Storage,
        bot_config: BotConfig,
        settings: CoreSettings,
        me: BotIdentity,
        formatter: LineFormatter,
    ) -> None:
        self._storage = storage
        self._bot_config = bot_config
        self._settings = settings
        self._me = me
        self.formatter = formatter

    async def system_prompt(self) -> str:
        return SYSTEM_TEMPLATE.format(
            platform=self._bot_config.platform,
            names=", ".join(await self._bot_config.names()) or "—",
            username=self._me.username,
            participants=participant_lines(self._settings.focus_users),
            persona=await self._bot_config.persona(),
        )

    async def for_reply(self, chat_id: int, new: list[StoredMessage], task: Callable[[IdMap], str]) -> Prompt:
        """`task` gets the numbering so it can point at a message ("#3")."""
        return await self._render(chat_id, await self._sections(chat_id, new), task)

    async def for_precheck(self, chat_id: int, new: list[StoredMessage]) -> Prompt:
        """The cheap first look before an unprompted comment: the same system prompt (a cache prefix
        shared with the full prompt), the last few lines and the new ones, no memory."""
        first_new = min(message.message_id for message in new)
        candidates = await self._storage.messages_before(chat_id, first_new, _PRECHECK_FETCH)
        window = self.formatter.newest_within(candidates, self._settings.precheck_context_tokens)
        sections = [(_RECENT, window)] if window else []
        sections.append((_NEW, new))
        return await self._render(chat_id, sections, lambda ids: PRECHECK_TASK, memory=False)

    async def for_digest(self, chat_id: int, since_ts: int) -> Prompt:
        week = await self._storage.messages_since(chat_id, since_ts, _DIGEST_FETCH)
        picked = self.formatter.newest_within(week, _DIGEST_TOKENS)
        sections = [(_RECENT, picked)] if picked else []
        return await self._render(chat_id, sections, lambda ids: DIGEST_TASK)

    async def _render(
        self,
        chat_id: int,
        sections: list[tuple[str, list[StoredMessage]]],
        task: Callable[[IdMap], str],
        *,
        memory: bool = True,
    ) -> Prompt:
        # The timeline is numbered first, so a message keeps its number from one prompt to the next:
        # the numbers are part of the cached prefix. Older and replied-to messages get the numbers after it.
        timeline = [m.message_id for title, messages in sections if title in _TIMELINE for m in messages]
        other = sorted(m.message_id for title, messages in sections if title not in _TIMELINE for m in messages)
        ids = IdMap(timeline + other)
        parts = []
        summary = await self._storage.get_summary(chat_id) if memory else None
        if summary:
            parts.append(f"ПАМЯТЬ ЧАТА:\n{summary.text}")
        parts += [f"{title}:\n{self.formatter.lines(messages, ids)}" for title, messages in sections]
        parts.append(task(ids))
        return Prompt(
            messages=[
                {"role": "system", "content": await self.system_prompt()},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            ids=ids,
        )

    async def _sections(self, chat_id: int, new: list[StoredMessage]) -> list[tuple[str, list[StoredMessage]]]:
        """Ordered for the provider's prompt cache: what stays the same from one batch to the next comes
        first (the system prompt, the summary, the recent window), what changes per batch comes last."""
        settings = self._settings
        fmt = self.formatter
        new_ids = {message.message_id for message in new}
        first_new = min(new_ids)

        # The window starts where the summary ends, so between folds it only grows at the end and the
        # whole prefix up to it is cached. The summarizer keeps that tail within RECENT_CONTEXT_TOKENS;
        # if it lags behind, fall back to the newest messages that fit.
        summary = await self._storage.get_summary(chat_id)
        candidates = await self._storage.messages_before(chat_id, first_new, _RECENT_FETCH)
        if summary:
            tail = [message for message in candidates if message.message_id > summary.upto_message_id]
            if sum(fmt.cost(message) for message in tail) <= settings.recent_context_tokens:
                candidates = tail
        window = fmt.newest_within(candidates, settings.recent_context_tokens)
        window_start = window[0].message_id if window else first_new
        shown = new_ids | {message.message_id for message in window}

        sections: list[tuple[str, list[StoredMessage]]] = [(_RECENT, window)] if window else []
        # Older messages of the focus users (always) and of whoever wrote the new messages:
        # the material for "you said the opposite last week".
        budgets = {user_id: settings.focus_history_tokens for user_id in settings.focus_users}
        for message in new:
            if not message.is_bot:
                budgets.setdefault(message.user_id, settings.author_history_tokens)
        for user_id, budget in budgets.items():
            older = await self._storage.user_messages_before(chat_id, user_id, window_start, _HISTORY_FETCH)
            picked = fmt.newest_within(older, budget)
            if picked:
                name = settings.focus_users.get(user_id) or picked[-1].author
                sections.append((f"ЧТО ПИСАЛИ РАНЬШЕ — {name}", picked))

        replied_ids = sorted({m.reply_to for m in new if m.reply_to is not None} - shown)
        replied = await self._storage.get_messages(chat_id, replied_ids)
        if replied:
            sections.append(("СООБЩЕНИЯ, НА КОТОРЫЕ ОТВЕТИЛИ", replied))
        sections.append((_NEW, new))
        return sections
