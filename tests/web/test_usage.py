from datetime import date, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest

from backseat.storage import LLMCall
from backseat.web import usage

MSK = ZoneInfo("Europe/Moscow")
TODAY = date(2026, 9, 30)


def at(day: int, hour: int = 12) -> int:
    return int(datetime(2026, 9, day, hour, tzinfo=MSK).timestamp())


def call(ts: int, purpose: str, cost: float, provider: str = "Relace", **extra: int) -> LLMCall:
    return LLMCall(ts, purpose, "deepseek", provider, cost=cost, **extra)


@pytest.mark.parametrize(
    ("value", "top"), [(0.0, 0.01), (0.0123, 0.02), (0.021, 0.025), (0.031, 0.05), (0.7, 1.0), (1.2, 2.0)]
)
def test_nice_axis_top(value: float, top: float) -> None:
    assert usage.nice_ceiling(value) == pytest.approx(top)


def test_report_groups_days_hosts_and_bots() -> None:
    calls = usage.all_calls(
        [
            (
                "Discord",
                [
                    call(at(29), "answer", 0.002, prompt_tokens=1000, cached_tokens=500, latency_ms=1000),
                    call(at(29), "comment", 0.001, prompt_tokens=1000, latency_ms=3000),
                    call(at(30), "precheck", 0.0001, "InferenceNet"),
                    call(at(30, 1), "summary", 0.003, "InferenceNet"),  # 01:00 MSK is still the 30th here
                    call(at(1, 12) - 40 * 86_400, "answer", 5.0),  # outside the period
                ],
            ),
            ("Telegram", [call(at(30), "picture", 0.02, None), call(at(30), "something new", 0.0)]),
        ]
    )
    report = usage.build(calls, days=7, today=TODAY, tz=MSK)
    assert report.total.calls == 6
    assert report.total.cost == pytest.approx(0.0261)
    assert report.active_days == 2  # recording began on the 29th: the average is not spread over 7 days
    assert report.per_day == pytest.approx(0.0261 / 2)
    assert [(key, slot) for key, _, slot, _ in report.groups] == [
        ("answers", 1),
        ("precheck", 2),
        ("memory", 3),
        ("pictures", 4),
        ("other", 6),  # the colour belongs to the group, not to its rank
    ]
    answers = report.groups[0][3]
    assert answers.calls == 2 and answers.cache_share == pytest.approx(0.25)
    assert answers.average_seconds == pytest.approx(2.0)
    assert [name for name, _ in report.providers] == ["Relace", "InferenceNet", "—"]
    assert [(name, tally.calls) for name, tally in report.bots] == [("Discord", 4), ("Telegram", 2)]

    bars = report.chart.bars
    assert [bar.day for bar in bars][-2:] == [date(2026, 9, 29), date(2026, 9, 30)]
    assert len(bars) == 7 and bars[-1].total.calls == 4
    assert bars[-2].by_group == {"answers": pytest.approx(0.003)}
    assert report.chart.ticks[-1].value == pytest.approx(0.025)
    # Stacked upwards from the baseline, in the groups' order.
    segments = bars[-1].segments
    assert [segment.group for segment in segments] == ["precheck", "memory", "pictures"]
    assert all(upper.y < lower.y for lower, upper in pairwise(segments))
    assert segments[0].y + segments[0].height == pytest.approx(report.chart.plot_bottom)
    assert report.chart.labels[-1] is bars[-1]  # the latest day is always labelled


def test_empty_report() -> None:
    report = usage.build([], days=30, today=TODAY, tz=MSK)
    assert report.total.calls == 0 and report.per_day == 0.0 and report.first_call_at is None
    assert report.groups == [] and all(not bar.segments for bar in report.chart.bars)
