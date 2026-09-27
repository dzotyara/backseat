from datetime import UTC, datetime

from aiogram.types import (
    Chat,
    Message,
    MessageEntity,
    MessageOriginUser,
    PhotoSize,
    Poll,
    PollOption,
    Sticker,
    User,
    Voice,
)

from backseat.telegram.messages import describe_message, find_address, is_trivial
from backseat.triggers import Address, compile_names
from tests.conftest import BOT, CHAT, IVAN

NAMES = compile_names(["бэксит", "ботяра"])
STICKER = Sticker(
    file_id="f", file_unique_id="u", type="regular", width=1, height=1, is_animated=False, is_video=False, emoji="😂"
)


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
    photo = [PhotoSize(file_id="p", file_unique_id="pu", width=1, height=1)]
    assert describe_message(tg_message("привет")) == "привет"
    assert describe_message(tg_message(None, sticker=STICKER)) == "[стикер 😂]"
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


def test_mention_after_emoji_uses_utf16_offsets() -> None:
    text = "😂 @Backseatyara_bot ты видел?"
    # "😂" is two UTF-16 code units, so the mention starts at offset 3, not 2.
    message = tg_message(text, entities=[MessageEntity(type="mention", offset=3, length=17)])
    assert find_address(message, BOT, NAMES) is Address.MENTION


def test_text_mention_by_id() -> None:
    bot_user = User(id=BOT.id, is_bot=True, first_name="Backseat")
    message = tg_message("эй ты", entities=[MessageEntity(type="text_mention", offset=3, length=2, user=bot_user)])
    assert find_address(message, BOT, NAMES) is Address.MENTION


def test_mention_in_photo_caption() -> None:
    message = tg_message(
        None,
        caption="@Backseatyara_bot что на фото?",
        caption_entities=[MessageEntity(type="mention", offset=0, length=17)],
    )
    assert find_address(message, BOT, NAMES) is Address.MENTION


def test_name_without_at() -> None:
    assert find_address(tg_message("ботяра, скажи честно"), BOT, NAMES) is Address.NAME


def test_reply_to_bot() -> None:
    bot_message = Message(
        message_id=0,
        date=datetime(2026, 9, 20, tzinfo=UTC),
        chat=Chat(id=CHAT, type="supergroup"),
        from_user=User(id=BOT.id, is_bot=True, first_name="Backseat"),
        text="шутка",
    )
    assert find_address(tg_message("да ну", reply_to_message=bot_message), BOT, NAMES) is Address.REPLY


def test_plain_message_is_not_addressed() -> None:
    assert find_address(tg_message("пойдём на шашлыки"), BOT, NAMES) is None


def test_bare_sticker_is_trivial_but_poll_is_not() -> None:
    assert is_trivial(tg_message(None, sticker=STICKER))
    assert is_trivial(tg_message("ахахах))"))
    assert not is_trivial(tg_message("кто в субботу?"))
    poll = Poll(
        id="p",
        question="Шашлыки?",
        options=[
            PollOption(persistent_id="1", text="да", voter_count=0),
            PollOption(persistent_id="2", text="нет", voter_count=0),
        ],
        total_voter_count=0,
        is_closed=False,
        is_anonymous=True,
        type="regular",
        allows_multiple_answers=False,
        allows_revoting=False,
        members_only=False,
    )
    assert not is_trivial(tg_message(None, poll=poll))
