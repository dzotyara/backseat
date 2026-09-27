"""The web panel's runtime overrides (BotConfig.runtime) as the responder and the LLM client see them."""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.llm import Completion, LLMClient, LLMError
from backseat.render import LineFormatter
from backseat.responder import Incoming, Responder
from backseat.storage import Storage, StoredMessage
from backseat.transport import Sent
from backseat.triggers import BotIdentity

CHAT = -1001
ME = BotIdentity(id=42, username="backseat_bot")
IVAN = 700
BASE_TS = 1_790_000_000
PROMPT = [{"role": "user", "content": "привет"}]


def make_settings(tmp_path: Path, **overrides: object) -> CoreSettings:
    persona = tmp_path / "persona.md"
    persona.write_text("Ты — тестовый бот.", encoding="utf-8")
    values: dict[str, object] = {
        "openrouter_api_key": "sk-test",
        "models": ["settings/model"],
        "persona_file": persona,
        "unprompted_cooldown_seconds": 60.0,
        "reactions_enabled": True,
        # Batches are processed by calling process() directly.
        "debounce_seconds": 999,
        "addressed_debounce_seconds": 999,
        "max_batch_wait_seconds": 999,
    }
    values.update(overrides)
    return CoreSettings(_env_file=None, **values)  # type: ignore[arg-type]


class FakeTransport:
    platform = "Telegram"
    max_length = 4000

    def __init__(self) -> None:
        self.sent: list[tuple[int | None, str]] = []
        self.reactions: list[tuple[int, str]] = []
        self._next_id = 10_000

    async def send(self, chat_id: int, text: str, *, reply_to: int | None = None, notify: bool = False) -> Sent:
        self._next_id += 1
        self.sent.append((reply_to, text))
        return Sent(message_id=self._next_id, created_at=BASE_TS + 100_000)

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        self.reactions.append((message_id, emoji))
        return True

    def typing(self, chat_id: int) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()


class FakeLLM:
    """Scripted answers; remembers each prompt and the models it was asked to walk."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.models: list[list[str] | None] = []

    async def complete(
        self, messages: list[dict[str, str]], *, models: list[str] | None = None, **_: object
    ) -> Completion:
        self.prompts.append("\n".join(part["content"] for part in messages))
        self.models.append(models)
        if not self.answers:
            raise LLMError("no scripted answer")
        return Completion(text=self.answers.pop(0), model="fake")


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = Storage(tmp_path / "bot.db")
    await store.connect()
    yield store
    await store.close()


def make_responder(
    settings: CoreSettings,
    storage: Storage,
    llm: object,
    transport: FakeTransport,
    *,
    bot_config: BotConfig,
    clock: Callable[[], float] = lambda: 1000.0,
    after_batch: object = None,
) -> Responder:
    formatter = LineFormatter(ZoneInfo(settings.timezone), {})
    context = ContextBuilder(storage, bot_config, settings, ME, formatter)
    return Responder(
        transport=transport,
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        bot_config=bot_config,
        me=ME,
        after_batch=after_batch,  # type: ignore[arg-type]
        clock=clock,
    )


async def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def add(storage: Storage, message_id: int, text: str, addressed: bool = False) -> Incoming:
    await storage.add_message(
        StoredMessage(CHAT, message_id, IVAN, "Иван", text, None, False, BASE_TS + message_id * 60)
    )
    return Incoming(message_id, IVAN, addressed=addressed, trivial=False)


async def test_paused_bot_remembers_the_chat_but_writes_nothing(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path)
    config = BotConfig(storage, settings)
    await config.set_runtime(paused=True)
    llm, transport, maintained = FakeLLM("ответ"), FakeTransport(), []

    async def after_batch(chat_id: int) -> None:
        maintained.append(chat_id)

    responder = make_responder(settings, storage, llm, transport, bot_config=config, after_batch=after_batch)
    responder.enqueue(CHAT, await add(storage, 5, "ботяра, ты тут?", addressed=True))
    responder.enqueue(CHAT, await add(storage, 6, "короче, я начал бегать"))
    await responder.process(CHAT)
    assert (llm.prompts, transport.sent, transport.reactions) == ([], [], [])

    # Back on: the next call is answered, and what was said during the pause is in the context.
    await config.set_runtime(paused=False)
    responder.enqueue(CHAT, await add(storage, 7, "ботяра, ну?", addressed=True))
    await responder.process(CHAT)
    assert [reply_to for reply_to, _ in transport.sent] == [7]
    assert "короче, я начал бегать" in llm.prompts[0]
    await wait_for(lambda: maintained == [CHAT, CHAT])  # the summary is maintained during the pause too
    await responder.shutdown()


async def test_runtime_models_reach_the_llm(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, unprompted_cooldown_seconds=0)
    config = BotConfig(storage, settings)
    await config.set_runtime(models=["panel/first", "panel/second"])
    llm, transport = FakeLLM("ответ", "SKIP"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport, bot_config=config)

    responder.enqueue(CHAT, await add(storage, 5, "ботяра, какая модель?", addressed=True))
    await responder.process(CHAT)
    responder.enqueue(CHAT, await add(storage, 6, "что-то содержательное"))
    await responder.process(CHAT)
    assert llm.models == [["panel/first", "panel/second"]] * 2
    await responder.shutdown()


async def test_runtime_cooldown_overrides_the_settings(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, unprompted_cooldown_seconds=1000)
    config = BotConfig(storage, settings)
    await config.set_runtime(unprompted_cooldown_seconds=0)
    llm, transport = FakeLLM("REPLY #1\nпервый", "REPLY #1\nвторой"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport, bot_config=config)

    responder.enqueue(CHAT, await add(storage, 5, "шашлыки в субботу?"))
    await responder.process(CHAT)
    responder.enqueue(CHAT, await add(storage, 6, "или в воскресенье?"))
    await responder.process(CHAT)
    assert [text for _, text in transport.sent] == ["первый", "второй"]  # settings would have said: cooling down

    await config.set_runtime(unprompted_cooldown_seconds=500)
    responder.enqueue(CHAT, await add(storage, 7, "а мясо кто берёт?"))
    await responder.process(CHAT)
    assert len(llm.prompts) == 2  # cooling down now: no request at all
    await responder.shutdown()


async def test_reactions_switch_changes_the_task_and_the_parser(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, unprompted_cooldown_seconds=0)
    config = BotConfig(storage, settings)
    await config.set_runtime(reactions_enabled=False)
    llm, transport = FakeLLM("REACT #1 🤡", "REACT #2 🤡"), FakeTransport()
    responder = make_responder(settings, storage, llm, transport, bot_config=config)

    responder.enqueue(CHAT, await add(storage, 5, "я гений"))
    await responder.process(CHAT)
    assert "REACT" not in llm.prompts[0]
    assert transport.reactions == []  # a REACT answer is a SKIP when reactions are off

    await config.set_runtime(reactions_enabled=None)  # back to the settings: on
    responder.enqueue(CHAT, await add(storage, 6, "я дважды гений"))
    await responder.process(CHAT)
    assert "REACT" in llm.prompts[1]
    assert transport.reactions == [(6, "🤡")]
    await responder.shutdown()


async def test_llm_walks_the_models_it_is_given(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "panel/gone":
            return httpx.Response(404, text="no such model")
        return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": "ок"}}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = LLMClient(make_settings(tmp_path), http=http, clock=lambda: 0.0)
    assert (await client.complete(PROMPT, models=["panel/gone", "panel/ok"])).model == "panel/ok"
    assert calls == ["panel/gone", "panel/ok"]

    calls.clear()
    await client.complete(PROMPT, models=["panel/gone", "panel/ok"])
    assert calls == ["panel/ok"]  # the cooldown after 404 holds for a given list too

    calls.clear()
    await client.complete(PROMPT)
    assert calls == ["settings/model"]
    await client.aclose()
