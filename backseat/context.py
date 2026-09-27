"""Assembles what the model sees: persona + memory + personal history + recent chat + the new messages."""

from backseat.chat_config import ChatConfig
from backseat.config import Settings
from backseat.prompts import DIGEST_TASK, PARTICIPANT_LINE, SYSTEM_TEMPLATE
from backseat.render import LineFormatter
from backseat.storage import Storage, StoredMessage
from backseat.triggers import BotIdentity

ChatMessages = list[dict[str, str]]

# How many rows to read before trimming to the token budgets.
_RECENT_FETCH = 600
_HISTORY_FETCH = 300
_DIGEST_FETCH = 3000
_DIGEST_TOKENS = 20000


class ContextBuilder:
    def __init__(
        self,
        storage: Storage,
        chat_config: ChatConfig,
        settings: Settings,
        me: BotIdentity,
        formatter: LineFormatter,
    ) -> None:
        self._storage = storage
        self._chat_config = chat_config
        self._settings = settings
        self._me = me
        self.formatter = formatter

    async def system_prompt(self, chat_id: int) -> str:
        participants = "".join(
            PARTICIPANT_LINE.format(name=name, user_id=user_id) for user_id, name in self._settings.focus_users.items()
        )
        return SYSTEM_TEMPLATE.format(
            names=", ".join(await self._chat_config.names(chat_id)) or "—",
            username=self._me.username,
            participants=participants,
            persona=await self._chat_config.persona(chat_id),
        )

    async def for_reply(self, chat_id: int, new: list[StoredMessage], task: str) -> ChatMessages:
        sections = await self._sections(chat_id, new)
        sections.append(task)
        return [
            {"role": "system", "content": await self.system_prompt(chat_id)},
            {"role": "user", "content": "\n\n".join(sections)},
        ]

    async def for_digest(self, chat_id: int, since_ts: int) -> ChatMessages:
        week = await self._storage.messages_since(chat_id, since_ts, _DIGEST_FETCH)
        sections = await self._memory_section(chat_id)
        picked = self.formatter.newest_within(week, _DIGEST_TOKENS)
        if picked:
            sections.append("ПОСЛЕДНЯЯ ПЕРЕПИСКА:\n" + self.formatter.lines(picked))
        sections.append(DIGEST_TASK)
        return [
            {"role": "system", "content": await self.system_prompt(chat_id)},
            {"role": "user", "content": "\n\n".join(sections)},
        ]

    async def _memory_section(self, chat_id: int) -> list[str]:
        summary = await self._storage.get_summary(chat_id)
        return [f"ПАМЯТЬ ЧАТА:\n{summary.text}"] if summary else []

    async def _sections(self, chat_id: int, new: list[StoredMessage]) -> list[str]:
        settings = self._settings
        fmt = self.formatter
        new_ids = {message.message_id for message in new}
        first_new = min(new_ids)

        candidates = await self._storage.messages_before(chat_id, first_new, _RECENT_FETCH)
        window = fmt.newest_within(candidates, settings.recent_context_tokens)
        window_start = window[0].message_id if window else first_new
        shown = new_ids | {message.message_id for message in window}

        sections = await self._memory_section(chat_id)

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
                sections.append(f"ЧТО ПИСАЛИ РАНЬШЕ — {name}:\n" + fmt.lines(picked))

        replied_ids = sorted({m.reply_to for m in new if m.reply_to is not None} - shown)
        replied = await self._storage.get_messages(chat_id, replied_ids)
        if replied:
            sections.append("СООБЩЕНИЯ, НА КОТОРЫЕ ОТВЕТИЛИ:\n" + fmt.lines(replied))

        if window:
            sections.append("ПОСЛЕДНЯЯ ПЕРЕПИСКА:\n" + fmt.lines(window))
        sections.append("НОВОЕ:\n" + fmt.lines(new))
        return sections
