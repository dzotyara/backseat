"""Spending: what the bots' model calls cost by day, what for and on which host, and the key's totals."""

import asyncio
import logging
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import Response

from backseat.storage import LLMCall
from backseat.web import bots, usage
from backseat.web.bots import BotSlot
from backseat.web.pages import PanelDep

log = logging.getLogger(__name__)
router = APIRouter()


async def _calls(slot: BotSlot, since_ts: int) -> tuple[list[LLMCall], str | None]:
    """The bot's calls since since_ts, or why there are none to show."""
    try:
        async with bots.open_bot(slot) as bot:
            if bot is None:
                return [], None
            return await bot.storage.llm_calls_since(since_ts), None
    except sqlite3.Error as exc:
        log.warning("%s: database unavailable: %s", slot.slug, exc)
        return [], f"{slot.title}: база сейчас не читается, её расходов на графике нет."


@router.get("/spending")
async def spending_page(request: Request, panel: PanelDep, days: int = usage.DEFAULT_PERIOD) -> Response:
    days = days if days in usage.PERIODS else usage.DEFAULT_PERIOD
    tz = panel.timezone
    today = datetime.fromtimestamp(panel.clock(), tz).date()
    since = usage.period_start(today, days)
    since_ts = int(datetime(since.year, since.month, since.day, tzinfo=tz).timestamp())
    slots = list(panel.slots.values())
    key, *found = await asyncio.gather(panel.spending.get(), *(_calls(slot, since_ts) for slot in slots))
    report = usage.build(
        usage.all_calls((slot.title, calls) for slot, (calls, _) in zip(slots, found, strict=True)),
        days=days,
        today=today,
        tz=tz,
    )
    problems = [problem for _, problem in found if problem]
    return panel.render(
        request,
        "spending.html",
        nav="spending",
        report=report,
        spending=key,
        problems=problems,
        periods=usage.PERIODS,
        group_titles=usage.GROUP_TITLES,
        groups=usage.GROUPS,
    )
