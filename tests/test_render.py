from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram.types import Chat, Message, MessageOriginUser, PhotoSize, Sticker, User, Voice

from backseat.render import LineFormatter, describe_message, estimate_tokens
from tests.conftest import BASE_TS, CHAT, IVAN, msg


def tg_message(text: str | None = None, **fields: object) -> Message:
    return Message(
        message_id=1,
        date=datetime(2026, 9, 20, tzinfo=UTC),
        chat=Chat(id=CHAT, type="supergroup"),
        from_user=User(id=IVAN, is_bot=False, first_name="Иван"),
        text=text,
        **fields,
    )


def test_describe_text_media_and_service_messages() -> None:
    sticker = Sticker(
        file_id="f",
        file_unique_id="u",
        type="regular",
        width=1,
        height=1,
        is_animated=False,
        is_video=False,
        emoji="😂",
    )
    photo = [PhotoSize(file_id="p", file_unique_id="pu", width=1, height=1)]
    assert describe_message(tg_message("привет")) == "привет"
    assert describe_message(tg_message(None, sticker=sticker)) == "[стикер 😂]"
    assert describe_message(tg_message(None, photo=photo, caption="мой велик")) == "[фото] мой велик"
    assert describe_message(tg_message(None, voice=Voice(file_id="v", file_unique_id="vu", duration=75))) == (
        "[голосовое 1:15]"
    )
    origin = MessageOriginUser(
        date=datetime(2026, 9, 1, tzinfo=UTC), sender_user=User(id=5, is_bot=False, first_name="X")
    )
    assert describe_message(tg_message("смотрите", forward_origin=origin)) == "[переслано] смотрите"
    joined = tg_message(None, new_chat_members=[User(id=7, is_bot=False, first_name="Новый")])
    assert describe_message(joined) == ""


def test_line_uses_alias_bot_label_and_reply_marker(formatter: LineFormatter) -> None:
    ivan = msg(5, "первая строка\n\nвторая", user_id=IVAN, author="Vanya 🚲", reply_to=3)
    assert formatter.line(ivan).endswith("Иван[700000001] ↩#3: первая строка / вторая")
    assert formatter.line(ivan).startswith("#5 ")
    assert " Ты: " in formatter.line(msg(6, "моя шутка", is_bot=True))


def test_long_text_is_truncated() -> None:
    short = LineFormatter(ZoneInfo("Europe/Moscow"), {}, max_chars=10)
    assert short.line(msg(1, "a" * 50)).endswith("aaaaaaaaa…")


def test_lines_group_by_day(formatter: LineFormatter) -> None:
    day = 24 * 3600
    text = formatter.lines([msg(1, ts=BASE_TS), msg(2, ts=BASE_TS + 60), msg(3, ts=BASE_TS + day)])
    headers = [line for line in text.splitlines() if line.startswith("—")]
    assert headers == ["— 20.09.2026 (вс) —", "— 21.09.2026 (пн) —"]


def test_newest_within_budget_keeps_the_latest(formatter: LineFormatter) -> None:
    messages = [msg(i, "x" * 60) for i in range(1, 11)]
    one_line = estimate_tokens(formatter.line(messages[-1]))
    picked = formatter.newest_within(messages, one_line * 3)
    assert [m.message_id for m in picked] == [8, 9, 10]
    assert formatter.newest_within(messages, 0) == []
