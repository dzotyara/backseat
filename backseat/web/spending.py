"""OpenRouter spending of the panel's API key (GET /key). The key itself never leaves this module."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5.0
CACHE_SECONDS = 60.0  # the dashboard is reloaded after every switch; OpenRouter doesn't need to know


@dataclass(frozen=True, slots=True)
class Spending:
    free_used: int | None = None
    free_limit: int | None = None
    usage_daily: float | None = None
    usage_monthly: float | None = None
    problem: str | None = None  # why there are no numbers, for the page


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def fetch_spending(
    base_url: str, api_key: SecretStr | None, transport: httpx.AsyncBaseTransport | None = None
) -> Spending:
    if api_key is None:
        return Spending(problem="OPENROUTER_API_KEY не задан — расходы показать не получится.")
    headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS) as http:
            response = await http.get(f"{base_url.rstrip('/')}/key", headers=headers)
        response.raise_for_status()
        data = response.json().get("data")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        log.warning("OpenRouter /key answered HTTP %s", status)
        if status in (401, 403):
            return Spending(problem="OpenRouter не принял ключ OPENROUTER_API_KEY.")
        return Spending(problem=f"OpenRouter ответил ошибкой {status}, попробуйте позже.")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        log.warning("OpenRouter /key unavailable: %s", type(exc).__name__)
        return Spending(problem="OpenRouter не отвечает, попробуйте позже.")
    if not isinstance(data, dict):
        return Spending(problem="OpenRouter прислал непонятный ответ.")
    free = data.get("free_model_daily_requests")
    free = free if isinstance(free, dict) else {}
    used, limit = _number(free.get("used")), _number(free.get("limit"))
    return Spending(
        free_used=None if used is None else int(used),
        free_limit=None if limit is None else int(limit),
        usage_daily=_number(data.get("usage_daily")),
        usage_monthly=_number(data.get("usage_monthly")),
    )


class SpendingCache:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._transport = transport
        self._clock = clock
        self._value: Spending | None = None
        self._fetched_at = 0.0

    async def get(self) -> Spending:
        if self._value is None or self._clock() - self._fetched_at >= CACHE_SECONDS:
            self._value = await fetch_spending(self._base_url, self._api_key, self._transport)
            self._fetched_at = self._clock()
        return self._value
