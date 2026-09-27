from backseat.storage import Storage
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
    assert await storage.active_group_chats(0) == [CHAT]


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
