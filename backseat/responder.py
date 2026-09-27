"""When and how Backseat speaks.

Messages are collected per chat and processed in batches after a short silence. A batch that
addresses the bot (@mention, name, reply) is always answered — the model is not asked whether
to reply. Otherwise the model may comment, react with an emoji, or stay silent.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.llm import LLMClient, LLMError
from backseat.prompts import (
    ADDRESSED_TASK,
    FALLBACK_REPLIES,
    REACT_OPTION,
    REACTION_EMOJIS,
    UNPROMPTED_TASK,
)
from backseat.replies import clean_reply, parse_action, split_message
from backseat.storage import Storage, StoredMessage
from backseat.transport import Sent, Transport
from backseat.triggers import BotIdentity

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Incoming:
    message_id: int
    user_id: int
    addressed: bool
    trivial: bool


@dataclass
class _ChatState:
    pending: list[Incoming] = field(default_factory=list)
    batch_started: float | None = None
    timer: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_comment_at: float = float("-inf")


class Responder:
    def __init__(
        self,
        *,
        transport: Transport,
        storage: Storage,
        llm: LLMClient,
        context: ContextBuilder,
        settings: CoreSettings,
        me: BotIdentity,
        after_batch: Callable[[int], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._storage = storage
        self._llm = llm
        self._context = context
        self._settings = settings
        self._me = me
        self._after_batch = after_batch
        self._clock = clock
        self._states: dict[int, _ChatState] = {}
        self._background: set[asyncio.Task[None]] = set()
        emojis = REACTION_EMOJIS if settings.reactions_enabled else ()
        self._emojis = emojis
        react_option = REACT_OPTION.format(emojis=" ".join(emojis)) if emojis else ""
        self._unprompted_task = UNPROMPTED_TASK.format(react_option=react_option)

    def _state(self, chat_id: int) -> _ChatState:
        return self._states.setdefault(chat_id, _ChatState())

    # --- batching ---

    def enqueue(self, chat_id: int, incoming: Incoming) -> None:
        settings = self._settings
        state = self._state(chat_id)
        state.pending.append(incoming)
        now = self._clock()
        if state.batch_started is None:
            state.batch_started = now
        addressed = any(item.addressed for item in state.pending)
        quiet = settings.addressed_debounce_seconds if addressed else settings.debounce_seconds
        delay = max(0.0, min(quiet, settings.max_batch_wait_seconds - (now - state.batch_started)))
        if len(state.pending) >= settings.max_batch_messages:
            delay = 0.0
        if state.timer and not state.timer.done():
            state.timer.cancel()
        state.timer = asyncio.create_task(self._fire(chat_id, delay))

    async def _fire(self, chat_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        state = self._state(chat_id)
        # From here on a new message must not cancel us mid-reply: it starts its own timer.
        if state.timer is asyncio.current_task():
            state.timer = None
        await self.process(chat_id)

    async def process(self, chat_id: int) -> None:
        state = self._state(chat_id)
        async with state.lock:
            if not state.pending:
                return
            batch, state.pending, state.batch_started = state.pending, [], None
            try:
                if any(item.addressed for item in batch):
                    await self._answer(chat_id, batch)
                else:
                    await self._maybe_comment(chat_id, batch)
            except Exception:
                log.exception("chat=%s failed to process a batch of %d", chat_id, len(batch))
        if self._after_batch:
            task = asyncio.create_task(self._after_batch(chat_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def shutdown(self) -> None:
        tasks = [s.timer for s in self._states.values() if s.timer] + list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- addressed: always answer ---

    async def _answer(self, chat_id: int, batch: list[Incoming]) -> None:
        messages = await self._storage.get_messages(chat_id, [item.message_id for item in batch])
        by_id = {message.message_id: message for message in messages}
        # One answer per person who called the bot, to their latest such message.
        targets: dict[int, int] = {}
        for item in batch:
            if item.addressed and item.message_id in by_id:
                targets[item.user_id] = item.message_id
        for target_id in sorted(targets.values()):
            target = by_id[target_id]
            author = self._context.formatter.author(target)
            prompt = await self._context.for_reply(
                chat_id,
                messages,
                lambda ids, target_id=target_id, author=author: ADDRESSED_TASK.format(
                    message_id=ids.short(target_id), author=author
                ),
            )
            text = ""
            async with self._transport.typing(chat_id):
                try:
                    completion = await self._llm.complete(prompt.messages)
                    text = clean_reply(completion.text)
                except LLMError as exc:
                    log.error("chat=%s every model failed for an addressed message: %s", chat_id, exc)
            if not text:
                text = random.choice(FALLBACK_REPLIES)
            await self.send(chat_id, text, reply_to=target_id, notify=True)
            log.info("chat=%s answered message=%s", chat_id, target_id)

    # --- not addressed: maybe comment or react ---

    async def _maybe_comment(self, chat_id: int, batch: list[Incoming]) -> None:
        state = self._state(chat_id)
        if all(item.trivial for item in batch):
            log.info("chat=%s skip: trivial batch of %d", chat_id, len(batch))
            return
        if self._clock() - state.last_comment_at < self._settings.unprompted_cooldown_seconds:
            log.info("chat=%s skip: cooldown", chat_id)
            return
        messages = await self._storage.get_messages(chat_id, [item.message_id for item in batch])
        if not any(not message.is_bot for message in messages):
            return
        prompt = await self._context.for_reply(chat_id, messages, lambda ids: self._unprompted_task)
        try:
            completion = await self._llm.complete(prompt.messages)
        except LLMError as exc:
            log.warning("chat=%s unprompted comment skipped, every model failed: %s", chat_id, exc)
            return
        numbers = [prompt.ids.short(message.message_id) for message in messages if not message.is_bot]
        action = parse_action(completion.text, [n for n in numbers if n is not None], self._emojis)
        target = prompt.ids.real(action.message_id)
        if action.kind == "reply" and target is not None:
            await self.send(chat_id, action.text, reply_to=target)
        elif action.kind == "react" and target is not None:
            await self._transport.react(chat_id, target, action.emoji)
        log.info("chat=%s unprompted=%s target=%s model=%s", chat_id, action.kind, target, completion.model)

    # --- output ---

    async def send(self, chat_id: int, text: str, reply_to: int | None = None, notify: bool = False) -> bool:
        """Post a message (split if too long), remember it as the bot's own, restart the cooldown."""
        for index, chunk in enumerate(split_message(text, self._transport.max_length)):
            first = index == 0  # only the first chunk is a reply
            sent = await self._transport.send(
                chat_id, chunk, reply_to=reply_to if first else None, notify=notify and first
            )
            if sent is None:
                return False
            await self.remember(chat_id, sent, chunk, reply_to if first else None)
        self._state(chat_id).last_comment_at = self._clock()
        return True

    async def remember(self, chat_id: int, sent: Sent, text: str, reply_to: int | None = None) -> None:
        """Store a message the bot posted, so it shows up as «Ты» in later prompts."""
        await self._storage.add_message(
            StoredMessage(
                chat_id=chat_id,
                message_id=sent.message_id,
                user_id=self._me.id,
                author=self._me.username or "bot",
                text=text,
                reply_to=reply_to,
                is_bot=True,
                created_at=sent.created_at,
            )
        )
