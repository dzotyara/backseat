from zoneinfo import ZoneInfo

from backseat.render import IdMap, LineFormatter
from tests.conftest import BASE_TS, IVAN, msg


def test_id_map_numbers_messages_from_one_in_the_order_given() -> None:
    snowflake = 1_300_000_000_000_000_000
    ids = IdMap([snowflake + 20, snowflake + 10, snowflake + 20])
    assert (ids.short(snowflake + 20), ids.short(snowflake + 10)) == (1, 2)
    assert (ids.real(1), ids.real(2)) == (snowflake + 20, snowflake + 10)
    assert ids.short(snowflake) is None
    assert ids.real(3) is None and ids.real(None) is None


def test_line_uses_alias_bot_label_and_reply_marker(formatter: LineFormatter) -> None:
    ids = IdMap([3, 5, 6])
    ivan = msg(5, "первая строка\n\nвторая", user_id=IVAN, author="Vanya 🚲", reply_to=3)
    assert formatter.line(ivan, ids).endswith("Иван[700000001] ↩#1: первая строка / вторая")
    assert formatter.line(ivan, ids).startswith("#2 ")
    assert " Ты: " in formatter.line(msg(6, "моя шутка", is_bot=True), ids)


def test_reply_to_a_message_not_shown_is_a_bare_marker(formatter: LineFormatter) -> None:
    assert formatter.line(msg(5, "согласен", reply_to=3), IdMap([5])) == "#1 15:05 Петя[111] ↩: согласен"


def test_long_text_is_truncated() -> None:
    short = LineFormatter(ZoneInfo("Europe/Moscow"), {}, max_chars=10)
    assert short.line(msg(1, "a" * 50), IdMap([1])).endswith("aaaaaaaaa…")


def test_lines_group_by_day(formatter: LineFormatter) -> None:
    day = 24 * 3600
    messages = [msg(1, ts=BASE_TS), msg(2, ts=BASE_TS + 60), msg(3, ts=BASE_TS + day)]
    text = formatter.lines(messages, IdMap([1, 2, 3]))
    headers = [line for line in text.splitlines() if line.startswith("—")]
    assert headers == ["— 20.09.2026 (вс) —", "— 21.09.2026 (пн) —"]


def test_newest_within_budget_keeps_the_latest(formatter: LineFormatter) -> None:
    messages = [msg(i, "x" * 60) for i in range(1, 11)]
    one_line = formatter.cost(messages[-1])
    picked = formatter.newest_within(messages, one_line * 3)
    assert [m.message_id for m in picked] == [8, 9, 10]
    assert formatter.newest_within(messages, 0) == []
