import time
from unittest.mock import AsyncMock, patch

import pytest

import bot as bot_module
from bot import ChatState, _truncate, serialize_new


def make_msg(id_, author, text, forced=False):
    return {"id": id_, "author": author, "text": text, "forced": forced}


def test_serialize_new_collapses_consecutive_duplicates():
    batch = [
        make_msg(1, "Никита", "@bot здарова"),
        make_msg(2, "Никита", "@bot здарова"),
        make_msg(3, "Никита", "@bot здарова"),
        make_msg(4, "Андрей", "прив"),
    ]
    out = serialize_new(batch)
    lines = out.splitlines()
    assert lines[0] == "1-3|Никита|@bot здарова|x3"
    assert lines[1] == "4|Андрей|прив"


def test_serialize_new_keeps_distinct_messages_separate():
    batch = [make_msg(1, "Никита", "привет"), make_msg(2, "Никита", "как дела")]
    out = serialize_new(batch)
    assert out.splitlines() == ["1|Никита|привет", "2|Никита|как дела"]


def test_truncate_adds_ellipsis_when_over_limit():
    text = "a" * 20
    result = _truncate(text, 10)
    assert len(result) == 10
    assert result.endswith("…")


def test_truncate_leaves_short_text_untouched():
    assert _truncate("короткий текст", 500) == "короткий текст"


@pytest.mark.asyncio
async def test_cooldown_skips_llm_call_and_keeps_context():
    chat_id = -1001
    state = bot_module.get_state(chat_id)
    state.pending = [make_msg(1, "Никита", "привет")]
    state.last_comment_at = time.monotonic()  # just commented -> cooldown active

    with patch.object(bot_module, "decide", new=AsyncMock()) as mock_decide, \
         patch.object(bot_module.bot, "send_message", new=AsyncMock()) as mock_send:
        await bot_module.process_batch(chat_id)

    mock_decide.assert_not_called()
    mock_send.assert_not_called()
    assert len(state.context) == 1
    assert state.pending == []


@pytest.mark.asyncio
async def test_mention_bypasses_cooldown_and_sends_reply():
    chat_id = -1002
    state = bot_module.get_state(chat_id)
    state.pending = [make_msg(5, "Никита", "@bot здарова", forced=True)]
    state.last_comment_at = time.monotonic()  # cooldown would normally block this

    fake_result = {"respond": True, "reply_to_message_id": 5, "comment": "привет!"}
    with patch.object(bot_module, "decide", new=AsyncMock(return_value=fake_result)) as mock_decide, \
         patch.object(bot_module.bot, "send_message", new=AsyncMock()) as mock_send:
        await bot_module.process_batch(chat_id)

    mock_decide.assert_called_once()
    mock_send.assert_called_once()
    _, kwargs = mock_send.call_args
    assert kwargs["reply_parameters"].message_id == 5


@pytest.mark.asyncio
async def test_invalid_reply_id_falls_back_to_last_message_in_batch():
    chat_id = -1003
    state = bot_module.get_state(chat_id)
    state.pending = [make_msg(10, "Андрей", "прив"), make_msg(11, "Андрей", "как сам")]
    state.last_comment_at = 0.0  # no cooldown

    fake_result = {"respond": True, "reply_to_message_id": 9999, "comment": "го"}
    with patch.object(bot_module, "decide", new=AsyncMock(return_value=fake_result)), \
         patch.object(bot_module.bot, "send_message", new=AsyncMock()) as mock_send:
        await bot_module.process_batch(chat_id)

    _, kwargs = mock_send.call_args
    assert kwargs["reply_parameters"].message_id == 11


@pytest.mark.asyncio
async def test_respond_false_sends_nothing_but_updates_context():
    chat_id = -1004
    state = bot_module.get_state(chat_id)
    state.pending = [make_msg(20, "Никита", "ок")]
    state.last_comment_at = 0.0

    fake_result = {"respond": False, "reply_to_message_id": None, "comment": None}
    with patch.object(bot_module, "decide", new=AsyncMock(return_value=fake_result)), \
         patch.object(bot_module.bot, "send_message", new=AsyncMock()) as mock_send:
        await bot_module.process_batch(chat_id)

    mock_send.assert_not_called()
    assert len(state.context) == 1
