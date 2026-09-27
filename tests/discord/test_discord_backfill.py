from datetime import UTC, datetime, timedelta

import pytest

from backseat.discord.backfill import BATCH_SIZE, backfill, fold_history
from backseat.llm import LLMError
from backseat.storage import Storage, StoredMessage
from tests.discord.fakes import BASE, BOT_ID, CHANNEL, FakeChannel, FakeSummarizer, make_message


def row(message_id: int, text: str) -> StoredMessage:
    return StoredMessage(
        chat_id=CHANNEL,
        message_id=message_id,
        user_id=1,
        author="Петя",
        text=text,
        reply_to=None,
        is_bot=False,
        created_at=int(BASE.timestamp()) + message_id,
    )


async def test_add_messages_upserts_in_one_go(storage: Storage) -> None:
    await storage.add_message(row(1, "старый текст"))
    await storage.add_messages([row(1, "новый текст"), row(2, "второй"), row(3, "третий")])
    await storage.add_messages([])
    assert [m.text for m in await storage.messages_after(CHANNEL, 0, 10)] == ["новый текст", "второй", "третий"]


async def test_backfill_stores_the_history_in_batches_once(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    count = 2 * BATCH_SIZE + 3
    history = [make_message(f"сообщение {i}", created_at=BASE + timedelta(minutes=i)) for i in range(count)]
    history[10] = make_message("Петя закрепил сообщение", system=True)
    history[20] = make_message("")  # a bare embed: nothing to remember
    history.append(make_message("и я тут", author_id=BOT_ID, author_name="Бэксит", bot=True))
    channel = FakeChannel(CHANNEL, history=history)
    batches: list[int] = []
    add_messages = storage.add_messages

    async def counting_add_messages(messages: list[StoredMessage]) -> None:
        batches.append(len(messages))
        await add_messages(messages)

    monkeypatch.setattr(storage, "add_messages", counting_add_messages)

    assert await backfill(channel, storage, BOT_ID, days=30) == 2 * BATCH_SIZE + 2
    assert batches == [BATCH_SIZE, BATCH_SIZE, 2]
    [call] = channel.history_calls
    assert call["limit"] is None and call["oldest_first"] is True
    assert abs(call["after"] - (datetime.now(UTC) - timedelta(days=30))) < timedelta(minutes=1)
    assert await storage.count_messages(CHANNEL) == 2 * BATCH_SIZE + 2
    assert await storage.get_message(CHANNEL, history[10].id) is None
    own = await storage.get_message(CHANNEL, history[-1].id)
    assert own is not None and own.is_bot
    assert await storage.get_meta(f"backfill:{CHANNEL}")

    # The next start does not read the history again.
    assert await backfill(channel, storage, BOT_ID, days=30) == 0
    assert len(channel.history_calls) == 1


async def test_backfill_can_be_switched_off(storage: Storage) -> None:
    channel = FakeChannel(CHANNEL, history=[make_message("привет")])
    assert await backfill(channel, storage, BOT_ID, days=0) == 0
    assert channel.history_calls == []
    assert await storage.get_meta(f"backfill:{CHANNEL}") is None


async def test_fold_history_runs_until_the_rest_fits() -> None:
    summarizer = FakeSummarizer(True, True, False)
    assert await fold_history(summarizer, CHANNEL, pause=0) == 2  # type: ignore[arg-type]
    assert summarizer.calls == [CHANNEL] * 3


async def test_fold_history_stops_when_every_model_fails() -> None:
    summarizer = FakeSummarizer(True, LLMError("всё упало"), True)
    assert await fold_history(summarizer, CHANNEL, pause=0) == 1  # type: ignore[arg-type]
    assert summarizer.calls == [CHANNEL] * 2
