import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from backseat.chat_config import ChatConfig
from backseat.config import Settings
from backseat.context import ContextBuilder
from backseat.llm import Completion, LLMError
from backseat.prompts import FALLBACK_REPLIES
from backseat.render import LineFormatter
from backseat.responder import Incoming, Responder
from backseat.storage import Storage
from tests.conftest import BOT, CHAT, IVAN, PETYA, FakeBot, FakeLLM, make_settings, msg

SLOW = {"debounce_seconds": 999, "addressed_debounce_seconds": 999, "max_batch_wait_seconds": 999}


def make_responder(
    settings: Settings,
    storage: Storage,
    llm: object,
    bot: FakeBot,
    clock: Callable[[], float] | None = None,
    after_batch: object = None,
) -> Responder:
    formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
    context = ContextBuilder(storage, ChatConfig(storage, settings), settings, BOT, formatter)
    return Responder(
        bot=bot,  # type: ignore[arg-type]
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        me=BOT,
        after_batch=after_batch,  # type: ignore[arg-type]
        typing=lambda chat_id: contextlib.nullcontext(),
        clock=clock or (lambda: 1000.0),
    )


async def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def add(storage: Storage, message_id: int, text: str, user_id: int = IVAN) -> Incoming:
    await storage.add_message(msg(message_id, text, user_id=user_id, author="Иван" if user_id == IVAN else "Петя"))
    return Incoming(message_id, user_id, addressed=False, trivial=False)


async def test_addressed_message_is_always_answered(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, bot = FakeLLM("Ты: Дисциплинированно заказываешь кроссовки."), FakeBot()
    responder = make_responder(settings, storage, llm, bot)
    await add(storage, 5, "ботяра, я же дисциплинированный?")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)

    assert [(s.reply_to, s.text) for s in bot.sent] == [(5, "Дисциплинированно заказываешь кроссовки.")]
    assert "К тебе обратились в сообщении #5 (автор — Иван[700000001])" in llm.prompt_text()
    remembered = await storage.get_message(CHAT, 10_001)
    assert remembered is not None and remembered.is_bot and remembered.reply_to == 5
    await responder.shutdown()


async def test_addressed_message_gets_a_fallback_when_every_model_fails(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    bot = FakeBot()
    responder = make_responder(settings, storage, FakeLLM(LLMError("all down")), bot)
    await add(storage, 5, "@Backseatyara_bot ау")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert bot.sent[0].reply_to == 5
    assert bot.sent[0].text in FALLBACK_REPLIES
    await responder.shutdown()


async def test_one_answer_per_person_who_called(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    bot = FakeBot()
    responder = make_responder(settings, storage, FakeLLM("раз", "два"), bot)
    await add(storage, 5, "ботяра")
    await add(storage, 6, "ботяра, ну?")
    await add(storage, 7, "ботяра, и мне ответь", user_id=PETYA)
    for message_id, user_id in ((5, IVAN), (6, IVAN), (7, PETYA)):
        responder.enqueue(CHAT, Incoming(message_id, user_id, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert [s.reply_to for s in bot.sent] == [6, 7]
    await responder.shutdown()


async def test_trivial_batch_costs_no_request(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm = FakeLLM()
    responder = make_responder(settings, storage, llm, FakeBot())
    await storage.add_message(msg(5, "[стикер 😂]"))
    responder.enqueue(CHAT, Incoming(5, PETYA, addressed=False, trivial=True))
    await responder.process(CHAT)
    assert llm.calls == []
    await responder.shutdown()


async def test_unprompted_reply_then_cooldown(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    now = [1000.0]
    llm, bot = FakeLLM("REPLY #5\nКроссовки уже в отставке.", "SKIP"), FakeBot()
    responder = make_responder(settings, storage, llm, bot, clock=lambda: now[0])

    responder.enqueue(CHAT, await add(storage, 5, "короче, бег это не моё"))
    await responder.process(CHAT)
    assert [(s.reply_to, s.text) for s in bot.sent] == [(5, "Кроссовки уже в отставке.")]

    now[0] += 10
    responder.enqueue(CHAT, await add(storage, 6, "буду гулять по вечерам"))
    await responder.process(CHAT)
    assert len(llm.calls) == 1  # still cooling down: no request at all

    now[0] += settings.unprompted_cooldown_seconds
    responder.enqueue(CHAT, await add(storage, 7, "или плавать"))
    await responder.process(CHAT)
    assert len(llm.calls) == 2
    assert len(bot.sent) == 1  # the model said SKIP
    await responder.shutdown()


async def test_unprompted_reaction(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    bot = FakeBot()
    responder = make_responder(settings, storage, FakeLLM("REACT #5 🤡"), bot)
    responder.enqueue(CHAT, await add(storage, 5, "я гений"))
    await responder.process(CHAT)
    assert [(r.message_id, r.emoji) for r in bot.reactions] == [(5, "🤡")]
    assert bot.sent == []
    await responder.shutdown()


async def test_a_new_message_neither_cancels_the_reply_in_flight_nor_gets_lost(
    settings: Settings, storage: Storage
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    answers = ["REPLY #5\nпервый", "второй"]

    class SlowLLM(FakeLLM):
        async def complete(self, messages: list[dict[str, str]], **_: object) -> Completion:
            self.calls.append(messages)
            started.set()
            await release.wait()
            return Completion(text=answers.pop(0), model="paid/model")

    bot = FakeBot()
    responder = make_responder(settings, storage, SlowLLM(), bot)  # zero debounce: timers fire at once
    responder.enqueue(CHAT, await add(storage, 5, "шашлыки в субботу?"))
    await asyncio.wait_for(started.wait(), 2)

    await add(storage, 6, "ботяра, а ты что думаешь?", user_id=PETYA)
    responder.enqueue(CHAT, Incoming(6, PETYA, addressed=True, trivial=False))
    await asyncio.sleep(0.05)
    release.set()

    await wait_for(lambda: len(bot.sent) == 2)
    assert [(s.reply_to, s.text) for s in bot.sent] == [(5, "первый"), (6, "второй")]
    await responder.shutdown()


async def test_after_batch_hook_runs(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    seen: list[int] = []

    async def hook(chat_id: int) -> None:
        seen.append(chat_id)

    responder = make_responder(settings, storage, FakeLLM("SKIP"), FakeBot(), after_batch=hook)
    responder.enqueue(CHAT, await add(storage, 5, "что-то содержательное"))
    await responder.process(CHAT)
    await wait_for(lambda: seen == [CHAT])
    await responder.shutdown()


async def test_long_reply_is_split_and_only_the_first_part_is_a_reply(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    bot = FakeBot()
    long_text = "\n".join(["абзац " * 100] * 10)
    responder = make_responder(settings, storage, FakeLLM(long_text), bot)
    await add(storage, 5, "ботяра, расскажи подробно")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert len(bot.sent) == 2
    assert [s.reply_to for s in bot.sent] == [5, None]
    await responder.shutdown()


async def test_bots_own_reply_shows_up_in_the_next_context(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, unprompted_cooldown_seconds=0, **SLOW)
    llm = FakeLLM("Ты опять про бег?", "REPLY #7\nи снова")
    # Telegram numbers messages sequentially, so the bot's reply to #5 becomes #6.
    responder = make_responder(settings, storage, llm, FakeBot(last_message_id=5))
    await add(storage, 5, "ботяра, угадай, о чём я")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)

    responder.enqueue(CHAT, await add(storage, 7, "да, опять про бег"))
    await responder.process(CHAT)
    assert "Ты ↩#5: Ты опять про бег?" in llm.prompt_text()
    await responder.shutdown()
