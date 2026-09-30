from backseat.storage import LLMCall, ModerationEntry, Storage
from tests.conftest import BASE_TS, CHAT, IVAN, msg


async def test_messages_roundtrip_and_edit(storage: Storage) -> None:
    await storage.add_message(msg(1, "привет", reply_to=None))
    await storage.edit_message(CHAT, 1, "привет всем", edited_at=BASE_TS + 5)
    stored = await storage.get_message(CHAT, 1)
    assert stored is not None
    assert stored.text == "привет всем"
    assert await storage.get_message(CHAT, 2) is None


async def test_ordering_and_windows(storage: Storage) -> None:
    for i in range(1, 8):
        user = IVAN if i % 2 else 111
        await storage.add_message(msg(i, f"m{i}", user_id=user, is_bot=(i == 7)))
    assert [m.message_id for m in await storage.messages_before(CHAT, 6, 3)] == [3, 4, 5]
    assert [m.message_id for m in await storage.messages_after(CHAT, 4, 10)] == [5, 6, 7]
    ivan = await storage.user_messages_before(CHAT, IVAN, 7, 10)
    assert [m.message_id for m in ivan] == [1, 3, 5]
    assert [m.message_id for m in await storage.get_messages(CHAT, [5, 2, 99])] == [2, 5]
    assert await storage.count_messages(CHAT) == 7
    assert await storage.count_messages(CHAT, since_ts=BASE_TS + 5 * 60) == 3
    assert await storage.active_chats(0) == [CHAT]


async def test_active_chats_are_all_chats_with_recent_messages(storage: Storage) -> None:
    channel = 1_300_000_000_000_000_000  # a Discord channel: positive, unlike Telegram groups
    await storage.add_message(msg(1, "давно", ts=BASE_TS - 3600))
    await storage.add_message(msg(1, "сегодня", ts=BASE_TS, chat_id=channel))
    assert sorted(await storage.active_chats(0)) == [CHAT, channel]
    assert await storage.active_chats(BASE_TS) == [channel]


async def test_bot_messages_are_excluded_from_personal_history(storage: Storage) -> None:
    await storage.add_message(msg(1, "шутка", is_bot=True))
    assert await storage.user_messages_before(CHAT, 42, 10, 10) == []


async def test_summary_settings_and_meta(storage: Storage) -> None:
    assert await storage.get_summary(CHAT) is None
    await storage.set_summary(CHAT, "сводка", 10, BASE_TS)
    await storage.set_summary(CHAT, "сводка 2", 20, BASE_TS + 1)
    summary = await storage.get_summary(CHAT)
    assert summary is not None
    assert (summary.text, summary.upto_message_id) == ("сводка 2", 20)

    await storage.set_setting(CHAT, "persona", "злой")
    assert await storage.get_setting(CHAT, "persona") == "злой"
    await storage.set_setting(CHAT, "persona", None)
    assert await storage.get_setting(CHAT, "persona") is None

    await storage.set_meta("digest:x", "sent")
    assert await storage.get_meta("digest:x") == "sent"


async def test_what_the_panel_reads(storage: Storage) -> None:
    channel = 1_300_000_000_000_000_000
    for i, (user_id, author) in enumerate([(IVAN, "Иван"), (IVAN, "Иван"), (111, "Петя")], start=1):
        await storage.add_message(msg(i, f"m{i}", user_id=user_id, author=author))
    await storage.add_message(msg(4, "я бот", is_bot=True))
    await storage.add_message(msg(5, "в канале", chat_id=channel))

    assert await storage.chat_activity() == [(channel, 1, BASE_TS + 5 * 60), (CHAT, 4, BASE_TS + 4 * 60)]
    assert await storage.chat_activity(CHAT) == [(CHAT, 4, BASE_TS + 4 * 60)]
    assert await storage.top_authors(CHAT, 5) == ["Иван", "Петя"]  # the bot is not one of them
    assert [m.message_id for m in await storage.latest_messages(CHAT, 2)] == [3, 4]

    await storage.set_summary(CHAT, "сводка", 2, BASE_TS)
    assert await storage.summary_times() == {CHAT: BASE_TS}
    for key, value in {"chat_title:-1001": "Чат", "chat_titles": "не то", "heartbeat": "1"}.items():
        await storage.set_meta(key, value)
    assert await storage.meta_with_prefix("chat_title:") == {"-1001": "Чат"}


async def test_llm_calls_and_moderation_log(storage: Storage) -> None:
    await storage.add_llm_call(LLMCall(BASE_TS, "answer", "m", "Relace", 100, 80, 5, 0.001, 900))
    await storage.add_llm_call(LLMCall(BASE_TS + 60, "precheck", "m"))
    assert [call.purpose for call in await storage.llm_calls_since(BASE_TS)] == ["answer", "precheck"]
    assert [call.purpose for call in await storage.llm_calls_since(BASE_TS + 1)] == ["precheck"]
    first = ModerationEntry(BASE_TS, CHAT, IVAN, "Иван", "забань Васю", "✅ Вася: забанен")
    second = ModerationEntry(BASE_TS, CHAT, IVAN, "Иван", "размуть Васю", "✅ Вася: мут снят")
    await storage.add_moderation(first)
    await storage.add_moderation(second)
    assert await storage.latest_moderation(5) == [second, first]  # the newest first, even within a second
    assert await storage.latest_moderation(1) == [second]
