import asyncio
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.llm import Completion, LLMError
from backseat.prompts import FALLBACK_REPLIES
from backseat.render import LineFormatter
from backseat.responder import Incoming, Responder
from backseat.storage import Storage
from tests.conftest import BOT, CHAT, IVAN, PETYA, FakeLLM, FakeTransport, make_settings, msg

SLOW = {"debounce_seconds": 999, "addressed_debounce_seconds": 999, "max_batch_wait_seconds": 999}


def make_responder(
    settings: CoreSettings,
    storage: Storage,
    llm: object,
    transport: FakeTransport,
    clock: Callable[[], float] | None = None,
    after_batch: object = None,
) -> Responder:
    formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
    bot_config = BotConfig(storage, settings)
    context = ContextBuilder(storage, bot_config, settings, BOT, formatter)
    return Responder(
        transport=transport,
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        bot_config=bot_config,
        me=BOT,
        after_batch=after_batch,  # type: ignore[arg-type]
        clock=clock or (lambda: 1000.0),
    )


async def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def add(storage: Storage, message_id: int, text: str, user_id: int = IVAN) -> Incoming:
    await storage.add_message(msg(message_id, text, user_id=user_id, author="Иван" if user_id == IVAN else "Петя"))
    return Incoming(message_id, user_id, addressed=False, trivial=False)


def numbered(prompt: str, text: str) -> str:
    """The number a message with this text got in the prompt, e.g. "#3"."""
    return next(line for line in prompt.splitlines() if line.endswith(f": {text}")).split(" ", 1)[0]


async def test_addressed_message_is_always_answered(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, transport = FakeLLM("Ты: Дисциплинированно заказываешь кроссовки."), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 4, "купил третьи кроссовки")
    await add(storage, 5, "ботяра, я же дисциплинированный?")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)

    sent = [(s.reply_to, s.text, s.notify) for s in transport.sent]
    assert sent == [(5, "Дисциплинированно заказываешь кроссовки.", True)]
    # The prompt numbers the shown messages from #1, oldest first: message 5 is the second one.
    assert "К тебе обратились в сообщении #2 (автор — Иван[700000001])" in llm.prompt_text()
    remembered = await storage.get_message(CHAT, 10_001)
    assert remembered is not None and remembered.is_bot and remembered.reply_to == 5
    await responder.shutdown()


async def test_addressed_message_gets_a_fallback_when_every_model_fails(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    transport = FakeTransport()
    responder = make_responder(settings, storage, FakeLLM(LLMError("all down")), transport)
    await add(storage, 5, "@Backseatyara_bot ау")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert transport.sent[0].reply_to == 5
    assert transport.sent[0].text in FALLBACK_REPLIES
    await responder.shutdown()


async def test_one_answer_per_person_who_called(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, transport = FakeLLM("раз", "два"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 5, "ботяра")
    await add(storage, 6, "ботяра, ну?")
    await add(storage, 7, "ботяра, и мне ответь", user_id=PETYA)
    for message_id, user_id in ((5, IVAN), (6, IVAN), (7, PETYA)):
        responder.enqueue(CHAT, Incoming(message_id, user_id, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert [s.reply_to for s in transport.sent] == [6, 7]
    assert "К тебе обратились в сообщении #2 " in llm.prompt_text(0)
    assert "К тебе обратились в сообщении #3 " in llm.prompt_text(1)
    await responder.shutdown()


async def test_trivial_batch_costs_no_request(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm = FakeLLM()
    responder = make_responder(settings, storage, llm, FakeTransport())
    await storage.add_message(msg(5, "[стикер 😂]"))
    responder.enqueue(CHAT, Incoming(5, PETYA, addressed=False, trivial=True))
    await responder.process(CHAT)
    assert llm.calls == []
    await responder.shutdown()


async def test_unprompted_reply_then_cooldown(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    now = [1000.0]
    llm, transport = FakeLLM("REPLY #1\nКроссовки уже в отставке.", "SKIP"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport, clock=lambda: now[0])

    responder.enqueue(CHAT, await add(storage, 5, "короче, бег это не моё"))
    await responder.process(CHAT)
    assert [(s.reply_to, s.text, s.notify) for s in transport.sent] == [(5, "Кроссовки уже в отставке.", False)]

    now[0] += 10
    responder.enqueue(CHAT, await add(storage, 6, "буду гулять по вечерам"))
    await responder.process(CHAT)
    assert len(llm.calls) == 1  # still cooling down: no request at all

    now[0] += settings.unprompted_cooldown_seconds
    responder.enqueue(CHAT, await add(storage, 7, "или плавать"))
    await responder.process(CHAT)
    assert len(llm.calls) == 2
    assert len(transport.sent) == 1  # the model said SKIP
    await responder.shutdown()


async def test_unprompted_reply_goes_to_the_message_the_model_numbered(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, transport = FakeLLM("REPLY #3\nА кто тогда за рулём?"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 500, "шашлыки в субботу?")
    batch = [
        await add(storage, 510, "я за"),
        await add(storage, 520, "я не пью", user_id=PETYA),
        await add(storage, 530, "мясо с меня"),
    ]
    for incoming in batch:
        responder.enqueue(CHAT, incoming)
    await responder.process(CHAT)

    assert numbered(llm.prompt_text(), "я не пью") == "#3"
    # An unmapped "3" is no message of the batch: parse_action would fall back to the newest one, 530.
    assert [(s.reply_to, s.text) for s in transport.sent] == [(520, "А кто тогда за рулём?")]
    await responder.shutdown()


async def test_unprompted_reaction(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    transport = FakeTransport()
    responder = make_responder(settings, storage, FakeLLM("REACT #1 🤡"), transport)
    responder.enqueue(CHAT, await add(storage, 5, "я гений"))
    await responder.process(CHAT)
    assert [(r.message_id, r.emoji) for r in transport.reactions] == [(5, "🤡")]
    assert transport.sent == []
    await responder.shutdown()


async def test_unprompted_reaction_goes_to_the_message_the_model_numbered(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, transport = FakeLLM("REACT #2 🔥", "REACT #1 🤡"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 500, "шашлыки в субботу?")
    for incoming in [await add(storage, 510, "мясо с меня"), await add(storage, 520, "а я торт", user_id=PETYA)]:
        responder.enqueue(CHAT, incoming)
    await responder.process(CHAT)
    assert numbered(llm.prompt_text(), "мясо с меня") == "#2"
    assert [(r.message_id, r.emoji) for r in transport.reactions] == [(510, "🔥")]

    # #1 is an older message, not a new one: the reaction goes to the newest message instead.
    responder.enqueue(CHAT, await add(storage, 540, "и мангал"))
    await responder.process(CHAT)
    assert (transport.reactions[-1].message_id, transport.reactions[-1].emoji) == (540, "🤡")
    await responder.shutdown()


async def test_a_new_message_neither_cancels_the_reply_in_flight_nor_gets_lost(
    settings: CoreSettings, storage: Storage
) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    answers = ["REPLY #1\nпервый", "второй"]

    class SlowLLM(FakeLLM):
        async def complete(self, messages: list[dict[str, str]], **_: object) -> Completion:
            self.calls.append(messages)
            started.set()
            await release.wait()
            return Completion(text=answers.pop(0), model="paid/model")

    transport = FakeTransport()
    responder = make_responder(settings, storage, SlowLLM(), transport)  # zero debounce: timers fire at once
    responder.enqueue(CHAT, await add(storage, 5, "шашлыки в субботу?"))
    await asyncio.wait_for(started.wait(), 2)

    await add(storage, 6, "ботяра, а ты что думаешь?", user_id=PETYA)
    responder.enqueue(CHAT, Incoming(6, PETYA, addressed=True, trivial=False))
    await asyncio.sleep(0.05)
    release.set()

    await wait_for(lambda: len(transport.sent) == 2)
    assert [(s.reply_to, s.text) for s in transport.sent] == [(5, "первый"), (6, "второй")]
    await responder.shutdown()


async def test_after_batch_hook_runs(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    seen: list[int] = []

    async def hook(chat_id: int) -> None:
        seen.append(chat_id)

    responder = make_responder(settings, storage, FakeLLM("SKIP"), FakeTransport(), after_batch=hook)
    responder.enqueue(CHAT, await add(storage, 5, "что-то содержательное"))
    await responder.process(CHAT)
    await wait_for(lambda: seen == [CHAT])
    await responder.shutdown()


async def test_long_reply_is_split_and_only_the_first_part_is_a_reply(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    transport = FakeTransport()
    long_text = "\n".join(["абзац " * 100] * 10)
    responder = make_responder(settings, storage, FakeLLM(long_text), transport)
    await add(storage, 5, "ботяра, расскажи подробно")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert all(len(s.text) <= transport.max_length for s in transport.sent)
    assert [(s.reply_to, s.notify) for s in transport.sent] == [(5, True), (None, False)]
    await responder.shutdown()


async def test_bots_own_reply_shows_up_in_the_next_context(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, unprompted_cooldown_seconds=0, **SLOW)
    llm, transport = FakeLLM("Ты опять про бег?", "REPLY #3\nи снова"), FakeTransport(last_message_id=5)
    # Telegram numbers messages sequentially, so the bot's reply to 5 becomes 6.
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 5, "ботяра, угадай, о чём я")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)

    responder.enqueue(CHAT, await add(storage, 7, "да, опять про бег"))
    await responder.process(CHAT)
    prompt = llm.prompt_text()
    assert "Ты ↩#1: Ты опять про бег?" in prompt
    assert numbered(prompt, "Ты опять про бег?") == "#2"
    assert transport.sent[-1].reply_to == 7
    await responder.shutdown()


async def test_precheck_no_costs_only_the_short_request(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, precheck_context_tokens=500, **SLOW)
    llm, transport = FakeLLM("НЕТ"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await storage.set_summary(CHAT, "Иван обещал бегать.", 0, 0)
    responder.enqueue(CHAT, await add(storage, 5, "пойду спать"))
    await responder.process(CHAT)
    assert len(llm.calls) == 1
    assert "Иван обещал бегать" not in llm.prompt_text()  # no memory in the short request
    assert llm.prompt_text().endswith("Ответь одним словом: ДА или НЕТ.")
    assert llm.options[0]["max_tokens"] == 5
    assert transport.sent == [] and transport.reactions == []
    await responder.shutdown()


async def test_precheck_yes_leads_to_the_full_decision(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, precheck_context_tokens=500, **SLOW)
    llm, transport = FakeLLM("Да.", "REPLY #1\nС понедельника, как обычно?"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await storage.set_summary(CHAT, "Иван обещал бегать.", 0, 0)
    responder.enqueue(CHAT, await add(storage, 5, "всё, завтра начинаю бегать"))
    await responder.process(CHAT)
    assert len(llm.calls) == 2
    assert "ПАМЯТЬ ЧАТА:\nИван обещал бегать." in llm.prompt_text(1)
    assert [(s.reply_to, s.text) for s in transport.sent] == [(5, "С понедельника, как обычно?")]
    await responder.shutdown()


async def test_failed_precheck_is_a_skip(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, precheck_context_tokens=500, **SLOW)
    llm, transport = FakeLLM(LLMError("all down")), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    responder.enqueue(CHAT, await add(storage, 5, "всё, завтра начинаю бегать"))
    await responder.process(CHAT)
    assert len(llm.calls) == 1
    assert transport.sent == []
    await responder.shutdown()


async def test_calls_to_the_bot_skip_the_precheck(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, precheck_context_tokens=500, **SLOW)
    llm, transport = FakeLLM("Тут я."), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 5, "ботяра, ты тут?")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert len(llm.calls) == 1
    assert "ДА или НЕТ" not in llm.prompt_text()
    assert [(s.reply_to, s.text) for s in transport.sent] == [(5, "Тут я.")]
    await responder.shutdown()


async def test_freeze_is_per_person_and_says_not_ready_without_a_model_call(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, reply_freeze_seconds=30, **SLOW)
    now = [1000.0]
    llm, transport = FakeLLM("Ивану.", "Пете.", "Ивану снова."), FakeTransport()
    responder = make_responder(settings, storage, llm, transport, clock=lambda: now[0])

    async def call(message_id: int, user_id: int = IVAN) -> None:
        await add(storage, message_id, f"ботяра, вопрос {message_id}", user_id=user_id)
        responder.enqueue(CHAT, Incoming(message_id, user_id, addressed=True, trivial=False))
        await responder.process(CHAT)

    await call(1)
    now[0] += 10
    await call(2)  # 20 s of Ivan's freeze left
    await call(3)  # Ivan again: already told, silence
    await call(4, user_id=PETYA)  # Petya has no freeze of his own: a real answer
    now[0] += 20
    await call(5)  # Ivan's freeze is over

    assert [(s.reply_to, s.text) for s in transport.sent] == [
        (1, "Ивану."),
        (2, "Ещё не готов ответить, дай мне 20 сек."),
        (4, "Пете."),
        (5, "Ивану снова."),
    ]
    assert len(llm.calls) == 3
    assert await storage.get_message(CHAT, 10_002) is None  # "not ready" never reaches later prompts
    await responder.shutdown()


async def test_two_callers_in_one_batch_are_both_answered(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, reply_freeze_seconds=30, **SLOW)
    llm, transport = FakeLLM("Ивану.", "Пете."), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 1, "ботяра, ты тут?")
    await add(storage, 2, "ботяра, и мне ответь", user_id=PETYA)
    responder.enqueue(CHAT, Incoming(1, IVAN, addressed=True, trivial=False))
    responder.enqueue(CHAT, Incoming(2, PETYA, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert [(s.reply_to, s.text) for s in transport.sent] == [(1, "Ивану."), (2, "Пете.")]
    await responder.shutdown()


async def test_a_nick_request_that_reached_the_chat_model_forbids_saying_done(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, **SLOW)
    llm, transport = FakeLLM("Это только для модераторов, дружище."), FakeTransport()
    responder = make_responder(settings, storage, llm, transport)
    await add(storage, 5, "ботяра, сделай мне ник Крутой")
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert "Запрещено писать «сделал», «готово»" in llm.prompt_text()

    await add(storage, 6, "ботяра, как дела?")
    responder.enqueue(CHAT, Incoming(6, IVAN, addressed=True, trivial=False))
    llm.answers.append("Норм.")
    await responder.process(CHAT)
    assert len(llm.calls) == 2 and "Запрещено писать" not in llm.prompt_text()
    await responder.shutdown()
