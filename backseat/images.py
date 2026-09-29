"""Pictures on request: «ботяра, нарисуй кота в танке». The chat model turns the request into an English
prompt and a caption in character; Pollinations draws it for free, one picture at a time (it refuses a
second request right after the first); OpenRouter's paid image models are a fallback only up to the
panel's daily cap."""

import asyncio
import json
import logging
import random
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from backseat.bot_config import Runtime
from backseat.llm import LLMClient, LLMError
from backseat.storage import Storage

log = logging.getLogger(__name__)

# Only messages that look like a drawing request cost the planning call; the rest are answered as usual.
REQUEST_RE = re.compile(
    r"нарису|рисан|рисун|картин|изобраз|сгенер|нагенер|пикч|\bарт\b|draw|picture|image|generate", re.IGNORECASE
)
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
FREE_GAP_SECONDS = 16.0  # Pollinations answers 402 to a request that comes right after the previous one
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Drawing:
    prompt: str  # English, for the image model
    caption: str  # posted with the picture


def parse_drawing(text: str) -> Drawing | None:
    """The planning answer; None = no drawing was asked for, or the answer is unreadable."""
    match = _JSON_RE.search(text)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("draw") is not True:
        return None
    prompt, caption = data.get("prompt"), data.get("caption")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    return Drawing(prompt.strip()[:1000], caption.strip()[:300] if isinstance(caption, str) else "")


class ImageMaker:
    def __init__(
        self,
        storage: Storage,
        llm: LLMClient,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gap: float = FREE_GAP_SECONDS,
    ) -> None:
        self._storage = storage
        self._llm = llm
        self._http = http or httpx.AsyncClient(timeout=90, follow_redirects=True)
        self._clock = clock
        self._sleep = sleep
        self._gap = gap
        self._lock = asyncio.Lock()
        self._queued = 0
        self._free_at = float("-inf")  # when Pollinations takes the next request

    @staticmethod
    def looks_like_request(text: str) -> bool:
        return bool(REQUEST_RE.search(text))

    async def plan(self, messages: list[dict[str, str]], runtime: Runtime) -> Drawing | None:
        """Ask the chat model for a prompt and a caption. LLMError propagates."""
        completion = await self._llm.complete(
            messages, max_tokens=400, temperature=0.7, models=runtime.models, providers=runtime.providers
        )
        return parse_drawing(completion.text)

    # --- daily limits (kept in meta, so a restart does not reset them) ---

    async def left_today(self, user_id: int, runtime: Runtime) -> bool:
        if runtime.images_per_user_per_day <= 0:
            return True
        return await self._count(f"user:{user_id}") < runtime.images_per_user_per_day

    async def count_drawn(self, user_id: int) -> None:
        await self._bump(f"user:{user_id}")

    async def _count(self, what: str) -> int:
        raw = await self._storage.get_meta(f"images:{time.strftime('%Y-%m-%d', time.gmtime())}:{what}")
        return int(raw) if raw and raw.isdigit() else 0

    async def _bump(self, what: str) -> None:
        key = f"images:{time.strftime('%Y-%m-%d', time.gmtime())}:{what}"
        await self._storage.set_meta(key, str(await self._count(what) + 1))

    # --- drawing ---

    async def draw(
        self, prompt: str, runtime: Runtime, on_queue: Callable[[int, int], Awaitable[None]] | None = None
    ) -> bytes | None:
        """The picture, or None if neither the free service nor an allowed paid model drew it.
        on_queue(position, seconds) is called once when the request has to wait its turn."""
        self._queued += 1
        try:
            position = self._queued
            wait = max(0.0, self._free_at - self._clock()) + (position - 1) * self._gap
            if on_queue is not None and wait > 3:
                await on_queue(position, round(wait))
            async with self._lock:
                image = await self._free(prompt)
        finally:
            self._queued -= 1
        if image is not None:
            return image
        if runtime.paid_images_per_day <= 0 or await self._count("paid") >= runtime.paid_images_per_day:
            return None
        try:
            image = await self._llm.generate_image(prompt, models=runtime.image_models)
        except LLMError as exc:
            log.warning("paid picture failed too: %s", exc)
            return None
        await self._bump("paid")
        return image

    async def _free(self, prompt: str) -> bytes | None:
        """Pollinations, with one retry after the pause it wants between requests. Call under the lock."""
        for attempt in range(2):
            wait = self._free_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
            image = await self._pollinations(prompt)
            self._free_at = self._clock() + self._gap
            if image is not None:
                return image
            log.info("Pollinations refused a picture (attempt %d)", attempt + 1)
        return None

    async def _pollinations(self, prompt: str) -> bytes | None:
        url = POLLINATIONS_URL.format(prompt=urllib.parse.quote(prompt, safe=""))
        params = {"width": 1024, "height": 1024, "nologo": "true", "safe": "true", "seed": random.randrange(10**9)}
        try:
            response = await self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            log.warning("Pollinations unavailable: %r", exc)
            return None
        if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
            return None
        return response.content

    async def aclose(self) -> None:
        await self._http.aclose()
