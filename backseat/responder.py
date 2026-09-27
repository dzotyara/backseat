"""When and how Backseat speaks.

Messages are collected per chat and processed in batches after a short silence. A batch that
addresses the bot (@mention, name, reply) is always answered — the model is not asked whether
to reply. Otherwise the model may comment, react with an emoji, or stay silent.
"""

import asyncio
import logging
import math
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from backseat.bot_config import BotConfig, Runtime
from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.llm import Completion, LLMClient, LLMError
from backseat.prompts import (
    ADDRESSED_TASK,
    FALLBACK_REPLIES,
    FREEZE_REPLY,
    REACT_OPTION,
    REACTION_EMOJIS,
    UNPROMPTED_TASK,
)
from backseat.replies import clean_reply, parse_action, split_message
from backseat.storage import Storage, StoredMessage
from backseat.transport import Sent, Transport
from backseat.triggers import BotIdentity

log = logging.getLogger(__name__)

_YES_RE = re.compile(r"\W*(?:да|yes)(?!\w)", re.IGNORECASE)  # the precheck's answer, "ДА" or "НЕТ"


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
    # user id -> no model-written answer to them before this (REPLY_FREEZE_SECONDS)
    frozen_until: dict[int, float] = field(default_factory=dict)
    told_frozen: set[int] = field(default_factory=set)  # who already got "not ready" during their freeze


def _unprompted_task(reactions_enabled: bool) -> tuple[str, tuple[str, ...]]:
    """The task for a batch nobody addressed to the bot, and the emojis it offers."""
    emojis = REACTION_EMOJIS if reactions_enabled else ()
    react_option = REACT_OPTION.format(emojis=" ".join(emojis)) if emojis else ""
    return UNPROMPTED_TASK.format(react_option=react_option), emojis


class Responder:
    def __init__(
        self,
        *,
        transport: Transport,
        storage: Storage,
        llm: LLMClient,
        context: ContextBuilder,
        settings: CoreSettings,
        bot_config: BotConfig,  # the runtime behaviour: .env defaults with the web panel's overrides
        me: BotIdentity,
        after_batch: Callable[[int], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._transport = transport
        self._storage = storage
        self._llm = llm
        self._context = context
        self._settings = settings
        self._bot_config = bot_config
        self._me = me
        self._after_batch = after_batch
        self._clock = clock
        self._states: dict[int, _ChatState] = {}
        self._background: set[asyncio.Task[None]] = set()

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
                runtime = await self._bot_config.runtime()
                if runtime.paused:
                    # The messages are stored already: a paused bot still remembers the chat.
                    log.info("chat=%s paused: %d message(s) left unanswered", chat_id, len(batch))
                elif any(item.addressed for item in batch):
                    await self._answer(chat_id, batch, runtime)
                else:
                    await self._maybe_comment(chat_id, batch, runtime)
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

    async def _complete(self, messages: list[dict[str, str]], runtime: Runtime, **options: Any) -> Completion:
        return await self._llm.complete(messages, models=runtime.models, **options)  # the panel's model order

    # --- addressed: always answer ---

    async def _answer(self, chat_id: int, batch: list[Incoming], runtime: Runtime) -> None:
        messages = await self._storage.get_messages(chat_id, [item.message_id for item in batch])
        by_id = {message.message_id: message for message in messages}
        # One answer per person who called the bot, to their latest such message.
        targets: dict[int, int] = {}
        for item in batch:
            if item.addressed and item.message_id in by_id:
                targets[item.user_id] = item.message_id
        state = self._state(chat_id)
        for target_id in sorted(targets.values()):
            target = by_id[target_id]
            left = state.frozen_until.get(target.user_id, float("-inf")) - self._clock()
            if left > 0:
                await self._tell_frozen(chat_id, target, left)
                continue
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
                    completion = await self._complete(prompt.messages, runtime)
                    text = clean_reply(completion.text)
                except LLMError as exc:
                    log.error("chat=%s every model failed for an addressed message: %s", chat_id, exc)
            if not text:
                text = random.choice(FALLBACK_REPLIES)
            await self.send(chat_id, text, reply_to=target_id, notify=True)
            self._freeze(state, target.user_id)
            log.info("chat=%s answered message=%s", chat_id, target_id)

    def _freeze(self, state: _ChatState, user_id: int) -> None:
        state.frozen_until[user_id] = self._clock() + self._settings.reply_freeze_seconds
        state.told_frozen.discard(user_id)

    async def _tell_frozen(self, chat_id: int, target: StoredMessage, left: float) -> None:
        """A canned "not ready yet", once per person per freeze: no model call, and it is not stored,
        so it neither shows up in later prompts nor restarts any cooldown."""
        state = self._state(chat_id)
        if target.user_id in state.told_frozen:
            log.info("chat=%s frozen: message=%s ignored, its author was already told", chat_id, target.message_id)
            return
        state.told_frozen.add(target.user_id)
        text = FREEZE_REPLY.format(seconds=math.ceil(left))
        await self._transport.send(chat_id, text, reply_to=target.message_id, notify=True)
        log.info("chat=%s frozen: told message=%s to wait %.0fs", chat_id, target.message_id, left)

    # --- not addressed: maybe comment or react ---

    async def _maybe_comment(self, chat_id: int, batch: list[Incoming], runtime: Runtime) -> None:
        state = self._state(chat_id)
        if all(item.trivial for item in batch):
            log.info("chat=%s skip: trivial batch of %d", chat_id, len(batch))
            return
        if self._clock() - state.last_comment_at < runtime.unprompted_cooldown_seconds:
            log.info("chat=%s skip: cooldown", chat_id)
            return
        messages = await self._storage.get_messages(chat_id, [item.message_id for item in batch])
        if not any(not message.is_bot for message in messages):
            return
        if self._settings.precheck_context_tokens > 0 and not await self._worth_a_look(chat_id, messages, runtime):
            return
        task, emojis = _unprompted_task(runtime.reactions_enabled)
        prompt = await self._context.for_reply(chat_id, messages, lambda ids: task)
        try:
            completion = await self._complete(prompt.messages, runtime)
        except LLMError as exc:
            log.warning("chat=%s unprompted comment skipped, every model failed: %s", chat_id, exc)
            return
        numbers = [prompt.ids.short(message.message_id) for message in messages if not message.is_bot]
        action = parse_action(completion.text, [n for n in numbers if n is not None], emojis)
        target = prompt.ids.real(action.message_id)
        if action.kind == "reply" and target is not None:
            await self.send(chat_id, action.text, reply_to=target)
            author = next(message.user_id for message in messages if message.message_id == target)
            self._freeze(state, author)
        elif action.kind == "react" and target is not None:
            await self._transport.react(chat_id, target, action.emoji)
        log.info("chat=%s unprompted=%s target=%s model=%s", chat_id, action.kind, target, completion.model)

    async def _worth_a_look(self, chat_id: int, messages: list[StoredMessage], runtime: Runtime) -> bool:
        """The cheap first look: a few recent lines, one word back. In a busy chat most batches end here."""
        prompt = await self._context.for_precheck(chat_id, messages)
        try:
            completion = await self._complete(prompt.messages, runtime, max_tokens=5, temperature=0.0)
        except LLMError as exc:
            log.warning("chat=%s precheck skipped, every model failed: %s", chat_id, exc)
            return False
        if _YES_RE.match(completion.text):
            return True
        log.info("chat=%s skip: precheck", chat_id)
        return False

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
