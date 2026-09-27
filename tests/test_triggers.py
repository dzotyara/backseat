from datetime import UTC, datetime

from aiogram.types import Chat, Message, MessageEntity, Poll, PollOption, Sticker, User

from backseat.triggers import Address, compile_names, find_address, is_trivial, is_trivial_text
from tests.conftest import BOT, CHAT, IVAN

NAMES = compile_names(["бэксит", "ботяра"])


def tg_message(text: str | None = None, **fields: object) -> Message:
    return Message(
        message_id=1,
        date=datetime(2026, 9, 20, tzinfo=UTC),
        chat=Chat(id=CHAT, type="supergroup"),
        from_user=User(id=IVAN, is_bot=False, first_name="Иван"),
        text=text,
        **fields,
    )


def test_names_match_russian_case_endings() -> None:
    assert NAMES is not None
    for text in ("ботяра, ты тут?", "спроси ботяру", "Бэкситу привет", "эй БОТЯРА", "бэксита позовите"):
        assert NAMES.search(text), text


def test_names_do_not_match_inside_other_words() -> None:
    assert NAMES is not None
    for text in ("работяра пришёл", "бэкситянин", "ботинки", "бот"):
        assert not NAMES.search(text), text


def test_short_names_match_exactly() -> None:
    pattern = compile_names(["бот"])
    assert pattern is not None
    assert pattern.search("эй бот, ответь")
    assert not pattern.search("ботинок")


def test_no_names_means_no_pattern() -> None:
    assert compile_names(["", "  "]) is None


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


def test_trivial_texts() -> None:
    for text in ("ок", "Ага!", "ахахах))", "да)", "+", "😂😂", "лол", "ну да", "ХАХА"):
        assert is_trivial_text(text), text
    for text in ("да, я против", "кто в субботу?", "ахах, ну ты даёшь"):
        assert not is_trivial_text(text), text


def test_bare_sticker_is_trivial_but_poll_is_not() -> None:
    sticker = Sticker(
        file_id="f", file_unique_id="u", type="regular", width=1, height=1, is_animated=False, is_video=False
    )
    assert is_trivial(tg_message(None, sticker=sticker))
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
