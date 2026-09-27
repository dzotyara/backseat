"""OpenRouter chat-completions client that walks an ordered list of models until one answers."""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from backseat.config import CoreSettings

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# After these failures the model is skipped for a while instead of being retried on every message:
# no credits, gated/forbidden, removed, rate-limited.
_COOLDOWN_SECONDS = {402: 600.0, 403: 3600.0, 404: 3600.0, 429: 60.0}


class LLMError(Exception):
    """Every configured model failed."""


class _ModelFailed(Exception):
    def __init__(self, reason: str, cooldown: float = 0.0) -> None:
        super().__init__(reason)
        self.cooldown = cooldown


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    finish_reason: str | None = None  # "length" means the answer hit max_tokens and was cut


class LLMClient:
    def __init__(
        self,
        settings: CoreSettings,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.models = list(settings.models)
        self.last_model: str | None = None
        self._max_tokens = settings.max_tokens
        self._reasoning = settings.reasoning
        self._base_url = settings.openrouter_base_url.rstrip("/")
        self._clock = clock
        self._skip_until: dict[str, float] = {}
        self._headers = {"Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}"}
        if settings.openrouter_app_name:
            self._headers["X-Title"] = settings.openrouter_app_name
        if settings.openrouter_site_url:
            self._headers["HTTP-Referer"] = settings.openrouter_site_url
        self._http = http or httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        models: list[str] | None = None,
    ) -> Completion:
        """Walk `models` (by default the configured ones) in order until one answers."""
        models = self.models if models is None else models
        now = self._clock()
        # If every model is cooling down, try them all anyway rather than go silent.
        candidates = [m for m in models if self._skip_until.get(m, 0.0) <= now] or models
        failures = []
        for model in candidates:
            try:
                completion = await self._request(model, messages, max_tokens or self._max_tokens, temperature)
            except _ModelFailed as exc:
                log.warning("Model %s failed: %s", model, exc)
                failures.append(f"{model}: {exc}")
                if exc.cooldown:
                    self._skip_until[model] = self._clock() + exc.cooldown
                continue
            self.last_model = model
            return completion
        raise LLMError("; ".join(failures) or "no models configured")

    async def _request(
        self,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> Completion:
        payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if self._reasoning == "off":
            payload["reasoning"] = {"enabled": False}
        else:
            payload["reasoning"] = {"effort": self._reasoning, "exclude": True}

        try:
            response = await self._http.post(f"{self._base_url}/chat/completions", json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise _ModelFailed(f"network error: {exc!r}") from exc
        if response.status_code != 200:
            raise _ModelFailed(
                f"HTTP {response.status_code}: {response.text[:300]}",
                _COOLDOWN_SECONDS.get(response.status_code, 0.0),
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise _ModelFailed("response is not JSON") from exc

        # OpenRouter may answer 200 with an error object when the upstream provider failed.
        if error := data.get("error"):
            code = error.get("code") if isinstance(error, dict) else None
            cooldown = _COOLDOWN_SECONDS.get(code, 0.0) if isinstance(code, int) else 0.0
            raise _ModelFailed(f"provider error: {str(error)[:300]}", cooldown)

        choices = data.get("choices") or []
        if not choices:
            raise _ModelFailed("no choices in response")
        content = (choices[0].get("message") or {}).get("content") or ""
        text = _THINK_RE.sub("", content).strip()
        if not text:
            raise _ModelFailed(f"empty content (finish_reason={choices[0].get('finish_reason')})")

        usage = data.get("usage") or {}
        log.info(
            "LLM model=%s prompt_tokens=%s completion_tokens=%s cost=%s",
            data.get("model") or model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("cost"),
        )
        return Completion(
            text=text,
            model=data.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost=usage.get("cost"),
            finish_reason=choices[0].get("finish_reason"),
        )

    async def key_info(self) -> dict[str, Any] | None:
        """Usage and free-tier counters of the API key (GET /key), or None if unavailable."""
        try:
            response = await self._http.get(f"{self._base_url}/key", headers=self._headers)
            response.raise_for_status()
            return response.json().get("data")
        except (httpx.HTTPError, ValueError):
            log.warning("Could not fetch OpenRouter key info", exc_info=True)
            return None

    async def aclose(self) -> None:
        await self._http.aclose()
