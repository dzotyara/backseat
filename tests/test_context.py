from pathlib import Path

from backseat.bot_config import BotConfig
from backseat.context import ContextBuilder
from backseat.render import LineFormatter
from backseat.storage import Storage
from tests.conftest import BASE_TS, BOT, CHAT, IVAN, make_settings, msg


async def test_reply_context_has_every_section(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=120)
    await storage.add_message(msg(1, "всё, с понедельника бегаю каждое утро", user_id=IVAN, author="Vanya"))
    await storage.add_message(msg(2, "ага, как в прошлый раз"))
    for i in range(3, 40):
        await storage.add_message(msg(i, f"болтовня номер {i}"))
    await storage.add_message(msg(40, "а я говорил", is_bot=True))
    await storage.set_summary(CHAT, "Иван обещал бегать по утрам.", 20, BASE_TS)
    new = [msg(50, "помнишь?", reply_to=2), msg(51, "бег не моё", user_id=IVAN, author="Vanya")]
    for message in new:
        await storage.add_message(message)

    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    system, user = await builder.for_reply(CHAT, new, "ЗАДАЧА")

    assert "Ты — тестовый бот. Стеби Ивана." in system["content"]
    assert "бэксит, ботяра" in system["content"]
    assert "@Backseatyara_bot" in system["content"]
    assert "Постоянный участник: Иван — telegram_id 700000001" in system["content"]

    body = user["content"]
    sections = {part.split(":\n", 1)[0]: part for part in body.split("\n\n")}
    assert sections["ПАМЯТЬ ЧАТА"].endswith("Иван обещал бегать по утрам.")
    assert "с понедельника бегаю" in sections["ЧТО ПИСАЛИ РАНЬШЕ — Иван"]
    assert "ЧТО ПИСАЛИ РАНЬШЕ — Петя" in sections
    assert "#2 " in sections["СООБЩЕНИЯ, НА КОТОРЫЕ ОТВЕТИЛИ"]
    recent = sections["ПОСЛЕДНЯЯ ПЕРЕПИСКА"]
    assert "Ты: а я говорил" in recent
    assert "#1 " not in recent  # too old for the window; it lives in the personal history instead
    assert "#50 " in sections["НОВОЕ"]
    assert "Иван[700000001]: бег не моё" in sections["НОВОЕ"]
    assert body.endswith("ЗАДАЧА")


async def test_empty_chat_context_is_just_the_new_messages(
    settings: object, storage: Storage, formatter: LineFormatter
) -> None:
    new = [msg(1, "первое сообщение")]
    await storage.add_message(new[0])
    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)  # type: ignore[arg-type]
    _, user = await builder.for_reply(CHAT, new, "ЗАДАЧА")
    assert user["content"].startswith("НОВОЕ:\n")
    assert "ПАМЯТЬ ЧАТА" not in user["content"]


async def test_digest_context_covers_the_week(settings: object, storage: Storage, formatter: LineFormatter) -> None:
    week_ago = BASE_TS - 7 * 24 * 3600
    await storage.add_message(msg(1, "древность", ts=week_ago - 3600))
    await storage.add_message(msg(2, "на этой неделе", ts=BASE_TS))
    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)  # type: ignore[arg-type]
    _, user = await builder.for_digest(CHAT, week_ago)
    assert "на этой неделе" in user["content"]
    assert "древность" not in user["content"]
    assert "Итоги недели" in user["content"]
