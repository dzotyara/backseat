from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.digest import WeeklyDigest
from backseat.llm import LLMError
from backseat.render import LineFormatter
from backseat.responder import Responder
from backseat.storage import Storage
from tests.conftest import BOT, CHAT, FakeLLM, FakeTransport, make_settings, msg

MSK = ZoneInfo("Europe/Moscow")
SUNDAY_EVENING = datetime(2026, 9, 27, 20, 0, tzinfo=MSK)
WEEK_AGO = int((SUNDAY_EVENING - timedelta(days=7)).timestamp())


def make_digest(settings: CoreSettings, storage: Storage, llm: FakeLLM, transport: FakeTransport) -> WeeklyDigest:
    formatter = LineFormatter(MSK, settings.focus_users)
    context = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    responder = Responder(
        transport=transport,
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        me=BOT,
    )
    return WeeklyDigest(storage=storage, llm=llm, context=context, responder=responder, settings=settings)  # type: ignore[arg-type]


async def fill_week(storage: Storage, count: int, chat_id: int = CHAT) -> None:
    for i in range(1, count + 1):
        ts = int((SUNDAY_EVENING - timedelta(days=1, minutes=i)).timestamp())
        await storage.add_message(msg(i, f"событие {i}", ts=ts, chat_id=chat_id))


def test_next_run_is_sunday_evening(settings: CoreSettings) -> None:
    digest = WeeklyDigest(storage=None, llm=None, context=None, responder=None, settings=settings)  # type: ignore[arg-type]
    wednesday = datetime(2026, 9, 23, 12, 0, tzinfo=MSK)
    assert digest.next_run(wednesday) == SUNDAY_EVENING
    assert digest.next_run(SUNDAY_EVENING - timedelta(minutes=1)) == SUNDAY_EVENING
    assert digest.next_run(SUNDAY_EVENING) == SUNDAY_EVENING + timedelta(days=7)
    utc_morning = datetime(2026, 9, 27, 5, 0, tzinfo=ZoneInfo("UTC"))
    assert digest.next_run(utc_morning) == SUNDAY_EVENING


async def test_digest_is_posted_once_per_week(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, digest_min_messages=3)
    await fill_week(storage, 5)
    llm, transport = FakeLLM("Итоги недели\n— Иван снова не побежал"), FakeTransport()
    digest = make_digest(settings, storage, llm, transport)

    await digest.post_all(SUNDAY_EVENING)
    await digest.post_all(SUNDAY_EVENING)
    assert [s.text for s in transport.sent] == ["Итоги недели\n— Иван снова не побежал"]
    assert transport.sent[0].reply_to is None
    assert len(llm.calls) == 1
    assert "событие 5" in llm.prompt_text() and "Пора подвести итоги недели" in llm.prompt_text()


async def test_every_active_chat_gets_its_digest_whatever_its_id(tmp_path: Path, storage: Storage) -> None:
    # Telegram group ids are negative, Discord channel ids positive: each bot keeps its own database.
    settings = make_settings(tmp_path, digest_min_messages=3)
    channel = 1_300_000_000_000_000_000
    await fill_week(storage, 5)
    await fill_week(storage, 5, chat_id=channel)
    transport = FakeTransport()
    llm = FakeLLM("Итоги недели\n— раз", "Итоги недели\n— два")
    await make_digest(settings, storage, llm, transport).post_all(SUNDAY_EVENING)
    assert sorted(s.chat_id for s in transport.sent) == [CHAT, channel]


async def test_quiet_and_foreign_chats_get_no_digest(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, digest_min_messages=3, allowed_chat_ids=[CHAT])
    await fill_week(storage, 2)
    await fill_week(storage, 10, chat_id=-2002)
    llm = FakeLLM()
    await make_digest(settings, storage, llm, FakeTransport()).post_all(SUNDAY_EVENING)
    assert llm.calls == []


async def test_failed_digest_is_retried(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, digest_min_messages=3)
    await fill_week(storage, 5)
    llm, transport = FakeLLM(LLMError("down"), "Итоги недели\n— всё тихо"), FakeTransport()
    digest = make_digest(settings, storage, llm, transport)
    await digest.post_all(SUNDAY_EVENING)
    assert transport.sent == []
    await digest.post_all(SUNDAY_EVENING)
    assert len(transport.sent) == 1


async def test_compose_cleans_the_post_and_lets_model_failures_through(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path)
    await fill_week(storage, 5)
    llm = FakeLLM("```\nИтоги недели\n— шашлыки\n```", "```\n```", LLMError("down"))
    digest = make_digest(settings, storage, llm, FakeTransport())

    assert await digest.compose(CHAT, WEEK_AGO) == "Итоги недели\n— шашлыки"
    assert llm.options[0] == {"max_tokens": 1500}
    assert await digest.compose(CHAT, WEEK_AGO) is None  # nothing left to post
    with pytest.raises(LLMError):
        await digest.compose(CHAT, WEEK_AGO)
