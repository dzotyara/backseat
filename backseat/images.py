"""Pictures on request: «ботяра, нарисуй кота в танке». The chat model turns the request into an English
prompt and a caption in character, then the cheapest artist that answers draws it, one picture at a time:
Cloudflare Workers AI (free within its daily allocation), Pollinations with a key (its better models, paid
in pollen), anonymous Pollinations (free, the weak Sana, ~16 s between requests). OpenRouter's image
models are a last paid fallback, only up to the panel's daily cap."""

import asyncio
import base64
import json
import logging
import random
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from backseat.bot_config import Runtime
from backseat.llm import LLMClient, LLMError
from backseat.storage import Storage

log = logging.getLogger(__name__)

# Only messages that look like a drawing request cost the planning call; the rest are answered as usual.
REQUEST_RE = re.compile(
    # "рису|рисов" also catches «перерисуй», «перерисовывай», «дорисуй»: «нарису» alone missed them.
    r"рису|рисов|рисан|рисун|картин|изобраз|сгенер|нагенер|пикч|\bарт\b|draw|picture|image|generate",
    re.IGNORECASE,
)
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"  # anonymous: only the weak Sana
POLLINATIONS_KEYED_URL = "https://gen.pollinations.ai/image/{prompt}"  # with a key: the models it allows
FREE_GAP_SECONDS = 16.0  # anonymous Pollinations answers 402 to a request right after the previous one
KEYED_SECONDS = 6.0  # about how long a picture takes with a key, for the queue estimate
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Drawing:
    prompt: str  # English, for the image model
    caption: str  # posted with the picture
    shape: str | None = None  # square | landscape | portrait when the request says so
    refused: bool = False  # a drawing request the model will not draw: caption is its refusal


def quota_renews_at(tz: ZoneInfo, now: datetime | None = None) -> str:
    """When Cloudflare's daily allocation renews — midnight UTC — on the chat's clock, "03:00"."""
    now = now or datetime.now(UTC)
    midnight = (now.astimezone(UTC) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return f"{midnight.astimezone(tz):%H:%M}"


def dimensions(shape: str, size: int) -> tuple[int, int]:
    """(width, height) for a shape and a long side, in multiples of 16 as image models like."""
    short = max(16, round(size * 9 / 16 / 16) * 16)
    size = max(16, round(size / 16) * 16)
    return {"landscape": (size, short), "portrait": (short, size)}.get(shape, (size, size))


def parse_drawing(text: str) -> Drawing | None:
    """The planning answer; None = no drawing was asked for, or the answer is unreadable."""
    match = _JSON_RE.search(text)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    refuse = data.get("refuse")
    if data.get("draw") is not True:
        if isinstance(refuse, str) and refuse.strip():
            return Drawing("", refuse.strip()[:300], refused=True)
        return None
    prompt, caption = data.get("prompt"), data.get("caption")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    shape = data.get("shape")
    return Drawing(
        prompt.strip()[:1000],
        caption.strip()[:300] if isinstance(caption, str) else "",
        shape if shape in ("square", "landscape", "portrait") else None,
    )


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
        pollinations_key: str | None = None,
        cloudflare: tuple[str, str, str] | None = None,  # (account id, token, model)
    ) -> None:
        self._storage = storage
        self._key = pollinations_key
        self._cloudflare = cloudflare
        self._cloudflare_out_on: str | None = None  # the UTC day its free allocation ran out
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
            messages,
            max_tokens=400,
            temperature=0.7,
            models=runtime.models,
            providers=runtime.providers,
            purpose="picture_plan",
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
        self,
        prompt: str,
        runtime: Runtime,
        on_queue: Callable[[int, int], Awaitable[None]] | None = None,
        shape: str | None = None,
    ) -> bytes | None:
        """The picture, or None if neither the free service nor an allowed paid model drew it.
        on_queue(position, seconds) is called once when the request has to wait its turn."""
        self._queued += 1
        try:
            position = self._queued
            if self._cloudflare or (runtime.image_fallbacks and self._key and runtime.pollinations_models):
                wait = (position - 1) * KEYED_SECONDS
            else:
                wait = max(0.0, self._free_at - self._clock()) + (position - 1) * self._gap
            if on_queue is not None and wait > 3:
                await on_queue(position, round(wait))
            async with self._lock:
                # Cloudflare's FLUX.1 Schnell takes no size: it draws only the 1024×1024 square, and without
                # the fallbacks it is the only artist.
                shape = (shape or runtime.image_shape) if runtime.image_fallbacks else "square"
                size = dimensions(shape, runtime.image_size)
                image = await self._workers_ai(prompt) if shape == "square" else None
                if image is None and runtime.image_fallbacks:
                    image = await self._keyed(prompt, runtime.pollinations_models, size)
                    if image is None:
                        image = await self._free(prompt, size)
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

    def quota_spent(self) -> bool:
        """Cloudflare's free allocation ran out today (UTC)."""
        return self._cloudflare_out_on == time.strftime("%Y-%m-%d", time.gmtime())

    async def _workers_ai(self, prompt: str) -> bytes | None:
        """Cloudflare Workers AI; None without an account, on any failure, and for the rest of a UTC day
        once its free allocation is spent (it would only keep refusing)."""
        if self._cloudflare is None:
            return None
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self._cloudflare_out_on == today:
            return None
        account, token, model = self._cloudflare
        try:
            response = await self._http.post(
                CLOUDFLARE_URL.format(account=account, model=model),
                headers={"Authorization": f"Bearer {token}"},
                json={"prompt": prompt, "steps": 4},  # FLUX.1 Schnell rejects any other field, even a seed
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Cloudflare Workers AI unavailable: %r", exc)
            return None
        image = (data.get("result") or {}).get("image") if isinstance(data, dict) else None
        if response.status_code == 200 and isinstance(image, str):
            log.info("Cloudflare picture model=%s", model)
            return base64.b64decode(image)
        errors = str(data.get("errors") if isinstance(data, dict) else data)[:300]
        if response.status_code == 429 or "allocation" in errors.lower() or "4006" in errors:
            self._cloudflare_out_on = today
            log.info("Cloudflare free allocation is spent for %s: %s", today, errors)
        else:
            log.warning("Cloudflare refused a picture: HTTP %s %s", response.status_code, errors)
        return None

    async def _keyed(self, prompt: str, models: list[str], size: tuple[int, int]) -> bytes | None:
        """The key's models in order; None without a key, or when every model failed (e.g. no pollen left)."""
        if not self._key:
            return None
        url = POLLINATIONS_KEYED_URL.format(prompt=urllib.parse.quote(prompt, safe=""))
        headers = {"Authorization": f"Bearer {self._key}"}
        for model in models:
            params = {
                "model": model,
                "width": size[0],
                "height": size[1],
                "safe": "true",
                "seed": random.randrange(10**9),
            }
            try:
                response = await self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("Pollinations %s unavailable: %r", model, exc)
                continue
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                log.info("Pollinations picture model=%s", model)
                return response.content
            log.warning("Pollinations %s refused: HTTP %s %.200s", model, response.status_code, response.text)
        return None

    async def _free(self, prompt: str, size: tuple[int, int]) -> bytes | None:
        """Pollinations, with one retry after the pause it wants between requests. Call under the lock."""
        for attempt in range(2):
            wait = self._free_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
            image = await self._pollinations(prompt, size)
            self._free_at = self._clock() + self._gap
            if image is not None:
                return image
            log.info("Pollinations refused a picture (attempt %d)", attempt + 1)
        return None

    async def _pollinations(self, prompt: str, size: tuple[int, int]) -> bytes | None:
        url = POLLINATIONS_URL.format(prompt=urllib.parse.quote(prompt, safe=""))
        params = {
            "width": size[0],
            "height": size[1],
            "nologo": "true",
            "safe": "true",
            "seed": random.randrange(10**9),
        }
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
