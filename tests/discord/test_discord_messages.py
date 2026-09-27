import discord

from backseat.discord.messages import (
    author_of,
    channel_allowed,
    describe_message,
    find_address,
    is_trivial,
    stored_message,
)
from backseat.triggers import Address, compile_names
from tests.discord.fakes import (
    BASE,
    BOT_ID,
    BOT_ROLE,
    CHANNEL,
    OTHER_BOT,
    OTHER_CHANNEL,
    PETYA,
    THREAD,
    attachment,
    make_message,
    make_poll,
    make_reference,
    snapshot,
    sticker,
    text_channel,
    thread,
)

NAMES = compile_names(["бэксит", "ботяра"])


def test_text_with_media_markers() -> None:
    message = make_message(
        "  зацените  ",
        attachments=[
            attachment("cat.png", "image/png"),
            attachment("clip.mp4", "video/mp4"),
            attachment("song.mp3", "audio/mpeg"),
            attachment("report.pdf", "application/pdf"),
            attachment("notes"),  # no content type at all
        ],
        stickers=[sticker("Wave")],
    )
    assert describe_message(message) == (
        "[картинка] [видео] [аудио] [файл report.pdf] [файл notes] [стикер Wave] зацените"
    )


def test_voice_message_shows_its_length() -> None:
    voice = attachment("voice-message.ogg", "audio/ogg", duration_secs=75.4, waveform="AAAA")
    assert describe_message(make_message(attachments=[voice])) == "[голосовое 1:15]"


def test_poll_and_forwarded_message() -> None:
    assert describe_message(make_message(poll=make_poll("Куда идём?", "Бар", "Кино"))) == (
        "[опрос: Куда идём? — Бар / Кино]"
    )
    forwarded = make_message(snapshots=[snapshot("новость дня", [attachment("a.png", "image/png")])])
    assert describe_message(forwarded) == "[переслано] [картинка] новость дня"


def test_system_and_empty_messages_are_not_remembered() -> None:
    assert describe_message(make_message("Петя закрепил сообщение", system=True)) == ""
    assert describe_message(make_message("   ")) == ""  # e.g. a bare embed


def test_mention_of_the_bot_or_of_its_role() -> None:
    assert find_address(make_message("@Бэксит глянь", mentions=[BOT_ID]), BOT_ID, NAMES) is Address.MENTION
    assert find_address(make_message("@Бэксит глянь", role_mentions=[BOT_ROLE]), BOT_ID, NAMES) is Address.MENTION
    someone_else = make_message("@Петя глянь", mentions=[PETYA], role_mentions=[BOT_ROLE + 1])
    assert find_address(someone_else, BOT_ID, NAMES) is None


def test_name_call() -> None:
    assert find_address(make_message("ботяру позовите"), BOT_ID, NAMES) is Address.NAME
    assert find_address(make_message("ботяра, ты тут?"), BOT_ID, None) is None


def test_reply_to_the_bot() -> None:
    to_bot = make_message(stickers=[sticker("Wave")], reference=make_reference(5, author_id=BOT_ID))
    assert find_address(to_bot, BOT_ID, NAMES) is Address.REPLY
    to_petya = make_message("ага", reference=make_reference(5, author_id=PETYA))
    assert find_address(to_petya, BOT_ID, NAMES) is None
    deleted = make_reference(5)
    deleted.resolved = discord.DeletedReferencedMessage(deleted)
    assert find_address(make_message("ага", reference=deleted), BOT_ID, NAMES) is None
    assert find_address(make_message("ага", reference=make_reference(5)), BOT_ID, NAMES) is None  # not resolved
    assert find_address(make_message("привет", dm=True), BOT_ID, NAMES) is None


def test_trivial_messages() -> None:
    assert is_trivial(make_message(stickers=[sticker("Wave")]))
    assert is_trivial(make_message(attachments=[attachment("cat.png", "image/png")]))
    assert is_trivial(make_message("ок"))
    assert is_trivial(make_message("ахахах"))
    assert is_trivial(make_message("да)"))
    assert not is_trivial(make_message("Кто идёт в бар?"))
    assert not is_trivial(make_message(poll=make_poll("Куда идём?", "Бар", "Кино")))
    assert not is_trivial(make_message(snapshots=[snapshot("завтра всё подорожает")]))


def test_author() -> None:
    assert author_of(make_message("привет")) == (PETYA, "Петя", False)
    assert author_of(make_message("новый трек", author_id=OTHER_BOT, author_name="Музыка", bot=True))[2]
    assert author_of(make_message("новый пост", webhook_id=7))[2]  # feeds and integrations post via webhooks


def test_stored_message() -> None:
    reply = make_message("согласен", reference=make_reference(42), channel=text_channel(CHANNEL))
    row = stored_message(reply, BOT_ID)
    assert row is not None
    assert (row.chat_id, row.message_id, row.user_id, row.author) == (CHANNEL, reply.id, PETYA, "Петя")
    assert (row.text, row.reply_to, row.is_bot, row.created_at) == ("согласен", 42, False, int(BASE.timestamp()))

    forward = stored_message(make_message(snapshots=[snapshot("пост")], reference=make_reference(42, forward=True)), 0)
    assert forward is not None and forward.reply_to is None
    starter = stored_message(make_message("тред", reference=make_reference(42, channel_id=OTHER_CHANNEL)), 0)
    assert starter is not None and starter.reply_to is None

    own = stored_message(make_message("я тут", author_id=BOT_ID, author_name="Бэксит", bot=True), BOT_ID)
    assert own is not None and own.is_bot
    assert stored_message(make_message("закрепил", system=True), BOT_ID) is None


def test_allowed_channels_and_their_threads() -> None:
    assert channel_allowed(text_channel(OTHER_CHANNEL), [])  # no list: every channel
    assert channel_allowed(text_channel(CHANNEL), [CHANNEL])
    assert channel_allowed(thread(THREAD, parent_id=CHANNEL), [CHANNEL])
    assert not channel_allowed(text_channel(OTHER_CHANNEL), [CHANNEL])
    assert not channel_allowed(thread(THREAD, parent_id=OTHER_CHANNEL), [CHANNEL])
