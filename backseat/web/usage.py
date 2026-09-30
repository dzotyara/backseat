"""The spending page's numbers: the bots' recorded model calls (Storage.llm_calls) by day, by what they
were for and by host, plus the geometry of the daily chart. Pure functions over the rows."""

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backseat.storage import LLMCall

PERIODS = (7, 30, 90)  # days the page offers
DEFAULT_PERIOD = 30

# Chart series in a fixed order: the order is the colour (--series-N), whatever the data holds.
GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("answers", "Ответы", frozenset({"answer", "comment"})),
    ("precheck", "Предпроверки", frozenset({"precheck"})),
    ("memory", "Память и итоги недели", frozenset({"summary", "digest"})),
    ("pictures", "Картинки", frozenset({"picture_plan", "picture"})),
    ("moderation", "Модерация", frozenset({"moderation"})),
    ("other", "Другое", frozenset()),
)
_GROUP_OF = {purpose: key for key, _, purposes in GROUPS for purpose in purposes}
GROUP_TITLES = {key: title for key, title, _ in GROUPS}


def group_of(purpose: str) -> str:
    return _GROUP_OF.get(purpose, "other")


@dataclass(frozen=True, slots=True)
class BotCall:
    bot: str  # the slot's title
    call: LLMCall


@dataclass(slots=True)
class Tally:
    """Calls added up: how many, what they cost, how much of the input came from the host's cache."""

    calls: int = 0
    cost: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    timed: int = 0  # calls that know their latency

    def add(self, call: LLMCall) -> None:
        self.calls += 1
        self.cost += call.cost or 0.0
        self.prompt_tokens += call.prompt_tokens or 0
        self.cached_tokens += call.cached_tokens or 0
        self.completion_tokens += call.completion_tokens or 0
        if call.latency_ms is not None:
            self.latency_ms += call.latency_ms
            self.timed += 1

    @property
    def cache_share(self) -> float | None:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else None

    @property
    def average_seconds(self) -> float | None:
        return self.latency_ms / self.timed / 1000 if self.timed else None


@dataclass(frozen=True, slots=True)
class Segment:
    group: str
    series: int  # 1-based colour slot
    cost: float
    y: float
    height: float


@dataclass(frozen=True, slots=True)
class Bar:
    day: date
    total: Tally
    by_group: dict[str, float]
    x: float
    width: float
    segments: list[Segment]


@dataclass(frozen=True, slots=True)
class Tick:
    value: float
    y: float


@dataclass(frozen=True, slots=True)
class Chart:
    width: int
    height: int
    plot_top: float
    plot_bottom: float
    plot_left: float
    plot_right: float
    bars: list[Bar]
    ticks: list[Tick]
    labels: list[Bar]  # the bars whose date is written under the axis


@dataclass(frozen=True, slots=True)
class Report:
    days: int
    since: date
    total: Tally
    groups: list[tuple[str, str, int, Tally]]  # key, title, colour slot, tally — only groups with calls
    providers: list[tuple[str, Tally]]  # the most calls first
    bots: list[tuple[str, Tally]]
    chart: Chart
    first_call_at: int | None  # when recording began, as far as this period shows
    active_days: int  # days since the first recorded call, within the period

    @property
    def per_day(self) -> float:
        return self.total.cost / self.active_days if self.active_days else 0.0


def period_start(today: date, days: int) -> date:
    return today - timedelta(days=days - 1)


def build(calls: Sequence[BotCall], *, days: int, today: date, tz: ZoneInfo) -> Report:
    since = period_start(today, days)
    total = Tally()
    per_day: dict[date, Tally] = defaultdict(Tally)
    per_day_group: dict[date, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    groups: dict[str, Tally] = defaultdict(Tally)
    providers: dict[str, Tally] = defaultdict(Tally)
    bots: dict[str, Tally] = defaultdict(Tally)
    first_at: int | None = None
    for item in calls:
        call = item.call
        day = datetime.fromtimestamp(call.at, tz).date()
        if day < since or day > today:
            continue
        first_at = call.at if first_at is None else min(first_at, call.at)
        group = group_of(call.purpose)
        for tally in (total, per_day[day], groups[group], providers[call.provider or "—"], bots[item.bot]):
            tally.add(call)
        per_day_group[day][group] += call.cost or 0.0

    first_day = datetime.fromtimestamp(first_at, tz).date() if first_at is not None else today
    return Report(
        days=days,
        since=since,
        total=total,
        groups=[
            (key, title, slot, groups[key]) for slot, (key, title, _) in enumerate(GROUPS, start=1) if groups[key].calls
        ],
        providers=sorted(providers.items(), key=lambda pair: -pair[1].calls),
        bots=sorted(bots.items()),
        chart=_chart([since + timedelta(days=i) for i in range(days)], per_day, per_day_group),
        first_call_at=first_at,
        active_days=(today - max(first_day, since)).days + 1 if first_at is not None else 0,
    )


# --- the chart ---

WIDTH, HEIGHT = 760, 260
PLOT_LEFT, PLOT_RIGHT, PLOT_TOP, PLOT_BOTTOM = 56.0, 8.0, 12.0, 28.0
GAP = 2.0  # between stacked segments and between bars: the surface shows through
_NICE = (1, 2, 2.5, 5)


def nice_ceiling(value: float) -> float:
    """The smallest 1/2/2.5/5 × 10^n at or above value (the axis top); 0.01 for nothing at all."""
    if value <= 0:
        return 0.01
    magnitude = 10.0 ** math.floor(math.log10(value))
    for step in (*_NICE, 10):
        if step * magnitude >= value * (1 - 1e-9):
            return step * magnitude
    return 10 * magnitude


def _chart(days: list[date], per_day: dict[date, Tally], per_group: dict[date, dict[str, float]]) -> Chart:
    top = nice_ceiling(max((per_day[day].cost for day in days if day in per_day), default=0.0))
    plot_width = WIDTH - PLOT_LEFT - PLOT_RIGHT
    plot_height = HEIGHT - PLOT_TOP - PLOT_BOTTOM
    baseline = HEIGHT - PLOT_BOTTOM
    slot = plot_width / len(days)
    width = max(slot - GAP, 1.0) if slot > 6 else max(slot - 1, 0.5)
    bars = []
    for index, day in enumerate(days):
        x = PLOT_LEFT + index * slot + (slot - width) / 2
        segments = []
        y = baseline
        for series, (key, _, _) in enumerate(GROUPS, start=1):
            cost = per_group.get(day, {}).get(key, 0.0)
            if cost <= 0:
                continue
            bottom = y - (GAP if segments else 0.0)  # the gap comes out of the segment above, not the axis
            y -= cost / top * plot_height
            drawn = max(bottom - y, 1.0)  # a sliver still shows
            segments.append(Segment(key, series, cost, bottom - drawn, drawn))
        by_group = {key: cost for key, cost in per_group.get(day, {}).items() if cost > 0}
        bars.append(Bar(day, per_day.get(day, Tally()), by_group, x, width, segments))
    # Round steps: 2 → 0.5 each, 2.5 and 5 → five of 0.5 and 1, 1 → 0.25.
    leading = round(top / 10.0 ** math.floor(math.log10(top)), 1)
    parts = 5 if leading in (2.5, 5) else 4
    ticks = [Tick(top * i / parts, baseline - plot_height * i / parts) for i in range(parts + 1)]
    every = max(1, round(len(days) / 8))
    labels = [bar for index, bar in enumerate(bars) if (len(bars) - 1 - index) % every == 0]
    return Chart(WIDTH, HEIGHT, PLOT_TOP, baseline, PLOT_LEFT, WIDTH - PLOT_RIGHT, bars, ticks, labels)


def all_calls(per_bot: Iterable[tuple[str, Sequence[LLMCall]]]) -> list[BotCall]:
    return [BotCall(bot, call) for bot, calls in per_bot for call in calls]
