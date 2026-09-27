from pathlib import Path

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.context import ContextBuilder
from backseat.render import LineFormatter
from backseat.storage import Storage
from tests.conftest import BASE_TS, BOT, CHAT, IVAN, make_settings, msg

# Discord message ids are 19-digit snowflakes; the model should never have to copy one.
SNOWFLAKE = 1_300_000_000_000_000_000


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
    prompt = await builder.for_reply(CHAT, new, lambda ids: f"ЗАДАЧА про #{ids.short(51)}")
    system, user = prompt.messages

    assert "Ты — участник группового чата в Telegram." in system["content"]
    assert "Ты — тестовый бот. Стеби Ивана." in system["content"]
    assert "бэксит, ботяра" in system["content"]
    assert "@Backseatyara_bot" in system["content"]
    assert "Постоянный участник: Иван — id 700000001" in system["content"]

    body = user["content"]
    sections = {part.split(":\n", 1)[0]: part for part in body.split("\n\n")}
    assert sections["ПАМЯТЬ ЧАТА"].endswith("Иван обещал бегать по утрам.")
    assert "с понедельника бегаю" in sections["ЧТО ПИСАЛИ РАНЬШЕ — Иван"]
    assert "ЧТО ПИСАЛИ РАНЬШЕ — Петя" in sections
    assert "#2 " in sections["СООБЩЕНИЯ, НА КОТОРЫЕ ОТВЕТИЛИ"]
    recent = sections["ПОСЛЕДНЯЯ ПЕРЕПИСКА"]
    assert "Ты: а я говорил" in recent
    assert "#1 " not in recent  # too old for the window; it lives in the personal history instead
    # Messages 1-40 are all shown somewhere, so the new 50 and 51 are numbered #41 and #42.
    assert "#41 " in sections["НОВОЕ"] and "↩#2: помнишь?" in sections["НОВОЕ"]
    assert "#42 " in sections["НОВОЕ"] and "Иван[700000001]: бег не моё" in sections["НОВОЕ"]
    assert body.endswith("ЗАДАЧА про #42")
    assert (prompt.ids.real(41), prompt.ids.real(42)) == (50, 51)


async def test_messages_are_numbered_from_one_and_map_back(
    settings: CoreSettings, storage: Storage, formatter: LineFormatter
) -> None:
    await storage.add_message(msg(SNOWFLAKE + 700, "кто на футбол?", ts=BASE_TS))
    new = [msg(SNOWFLAKE + 950, "я", user_id=IVAN, author="Vanya", reply_to=SNOWFLAKE + 700, ts=BASE_TS + 60)]
    await storage.add_message(new[0])
    builder = ContextBuilder(storage, BotConfig(storage, settings, platform="Discord"), settings, BOT, formatter)
    prompt = await builder.for_reply(CHAT, new, lambda ids: "ЗАДАЧА")
    system, user = prompt.messages

    assert "Ты — участник группового чата в Discord." in system["content"]
    assert "#1 15:00 Петя[111]: кто на футбол?" in user["content"]
    assert "#2 15:01 Иван[700000001] ↩#1: я" in user["content"]
    assert str(SNOWFLAKE + 700) not in user["content"] and str(SNOWFLAKE + 950) not in user["content"]
    assert (prompt.ids.real(1), prompt.ids.real(2), prompt.ids.real(3)) == (SNOWFLAKE + 700, SNOWFLAKE + 950, None)


async def test_reply_to_a_message_that_is_not_shown_has_a_bare_marker(
    settings: CoreSettings, storage: Storage, formatter: LineFormatter
) -> None:
    new = [msg(5, "согласен", reply_to=3)]  # 3 was never stored, e.g. it predates the bot
    await storage.add_message(new[0])
    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    prompt = await builder.for_reply(CHAT, new, lambda ids: "ЗАДАЧА")
    assert "#1 15:05 Петя[111] ↩: согласен" in prompt.messages[1]["content"]


async def test_empty_chat_context_is_just_the_new_messages(
    settings: CoreSettings, storage: Storage, formatter: LineFormatter
) -> None:
    new = [msg(1, "первое сообщение")]
    await storage.add_message(new[0])
    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    _, user = (await builder.for_reply(CHAT, new, lambda ids: "ЗАДАЧА")).messages
    assert user["content"].startswith("НОВОЕ:\n")
    assert "ПАМЯТЬ ЧАТА" not in user["content"]


async def test_digest_context_covers_the_week(
    settings: CoreSettings, storage: Storage, formatter: LineFormatter
) -> None:
    week_ago = BASE_TS - 7 * 24 * 3600
    await storage.add_message(msg(1, "древность", ts=week_ago - 3600))
    await storage.add_message(msg(2, "на этой неделе", ts=BASE_TS))
    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    prompt = await builder.for_digest(CHAT, week_ago)
    _, user = prompt.messages
    assert "#1 15:00 Петя[111]: на этой неделе" in user["content"]
    assert "древность" not in user["content"]
    assert "Итоги недели" in user["content"]
    assert prompt.ids.real(1) == 2


async def test_precheck_sees_only_the_latest_lines_and_no_memory(
    tmp_path: Path, storage: Storage, formatter: LineFormatter
) -> None:
    settings = make_settings(tmp_path, precheck_context_tokens=40)
    for i in range(1, 30):
        await storage.add_message(msg(i, f"болтовня номер {i}"))
    await storage.set_summary(CHAT, "Иван обещал бегать по утрам.", 10, BASE_TS)
    new = [msg(30, "я теперь марафонец", user_id=IVAN, author="Vanya")]
    await storage.add_message(new[0])

    builder = ContextBuilder(storage, BotConfig(storage, settings), settings, BOT, formatter)
    precheck = await builder.for_precheck(CHAT, new)
    full = await builder.for_reply(CHAT, new, lambda ids: "ЗАДАЧА")
    assert precheck.messages[0] == full.messages[0]  # the same system prompt: a cache prefix both share
    body = precheck.messages[1]["content"]
    assert "ПАМЯТЬ ЧАТА" not in body and "ЧТО ПИСАЛИ РАНЬШЕ" not in body
    assert "болтовня номер 29" in body and "болтовня номер 20" not in body
    assert "НОВОЕ:\n" in body and "Иван[700000001]: я теперь марафонец" in body
    assert body.endswith("Ответь одним словом: ДА или НЕТ.")
