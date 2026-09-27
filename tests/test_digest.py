import contextlib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backseat.bot_config import BotConfig
from backseat.config import Settings
from backseat.context import ContextBuilder
from backseat.digest import WeeklyDigest
from backseat.llm import LLMError
from backseat.render import LineFormatter
from backseat.responder import Responder
from backseat.storage import Storage
from tests.conftest import BOT, CHAT, FakeBot, FakeLLM, make_settings, msg

MSK = ZoneInfo("Europe/Moscow")
SUNDAY_EVENING = datetime(2026, 9, 27, 20, 0, tzinfo=MSK)


def make_digest(settings: Settings, storage: Storage, llm: FakeLLM, bot: FakeBot) -> WeeklyDigest:
    formatter = LineFormatter(MSK, settings.focus_users)
    context = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    responder = Responder(
        bot=bot,  # type: ignore[arg-type]
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=context,
        settings=settings,
        me=BOT,
        typing=lambda chat_id: contextlib.nullcontext(),
    )
    return WeeklyDigest(storage=storage, llm=llm, context=context, responder=responder, settings=settings)  # type: ignore[arg-type]


async def fill_week(storage: Storage, count: int, chat_id: int = CHAT) -> None:
    for i in range(1, count + 1):
        ts = int((SUNDAY_EVENING - timedelta(days=1, minutes=i)).timestamp())
        await storage.add_message(msg(i, f"событие {i}", ts=ts, chat_id=chat_id))


def test_next_run_is_sunday_evening(settings: Settings) -> None:
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
    llm, bot = FakeLLM("Итоги недели\n— Иван снова не побежал"), FakeBot()
    digest = make_digest(settings, storage, llm, bot)

    await digest.post_all(SUNDAY_EVENING)
    await digest.post_all(SUNDAY_EVENING)
    assert [s.text for s in bot.sent] == ["Итоги недели\n— Иван снова не побежал"]
    assert bot.sent[0].reply_to is None
    assert len(llm.calls) == 1


async def test_quiet_and_foreign_chats_get_no_digest(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, digest_min_messages=3, allowed_chat_ids=[CHAT])
    await fill_week(storage, 2)
    await fill_week(storage, 10, chat_id=-2002)
    llm = FakeLLM()
    await make_digest(settings, storage, llm, FakeBot()).post_all(SUNDAY_EVENING)
    assert llm.calls == []


async def test_failed_digest_is_retried(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path, digest_min_messages=3)
    await fill_week(storage, 5)
    llm, bot = FakeLLM(LLMError("down"), "Итоги недели\n— всё тихо"), FakeBot()
    digest = make_digest(settings, storage, llm, bot)
    await digest.post_all(SUNDAY_EVENING)
    assert bot.sent == []
    await digest.post_all(SUNDAY_EVENING)
    assert len(bot.sent) == 1
