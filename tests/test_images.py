import base64
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from backseat.bot_config import BotConfig, Runtime
from backseat.context import ContextBuilder
from backseat.images import ImageMaker, parse_drawing
from backseat.llm import LLMClient
from backseat.render import LineFormatter
from backseat.responder import Incoming, Responder
from backseat.storage import Storage
from backseat.transport import picture_filename
from tests.conftest import BOT, CHAT, IVAN, FakeLLM, FakeTransport, make_settings, msg

JPEG = b"\xff\xd8\xff\xe0 a picture"
PLAN = json.dumps({"draw": True, "prompt": "a cat driving a tank, cartoon", "caption": "Держи, танкист"})
SLOW = {"debounce_seconds": 999, "addressed_debounce_seconds": 999, "max_batch_wait_seconds": 999}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 1))
        self.now += seconds


def pollinations(*answers: int) -> tuple[httpx.AsyncClient, list[str]]:
    """Pollinations that answers these statuses in turn (200 = a picture)."""
    urls: list[str] = []
    queue = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        status = queue.pop(0) if queue else 200
        if status == 200:
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(status, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), urls


class PaidLLM(FakeLLM):
    def __init__(self, *answers: str) -> None:
        super().__init__(*answers)
        self.paid: list[str] = []

    async def generate_image(self, prompt: str, *, models: list[str]) -> bytes:
        self.paid.append(prompt)
        return b"\x89PNG paid"


async def runtime(tmp_path: Path, storage: Storage, **changes: object) -> Runtime:
    config = BotConfig(storage, make_settings(tmp_path))
    await config.set_runtime(**changes)
    return await config.runtime()


def test_parse_drawing() -> None:
    assert parse_drawing("```json\n" + PLAN + "\n```") is not None
    assert parse_drawing('{"draw": false}') is None
    assert parse_drawing('{"draw": true, "prompt": " "}') is None
    assert parse_drawing("нарисовал бы, но не буду") is None
    assert ImageMaker.looks_like_request("ботяра, нарисуй кота в танке")
    assert not ImageMaker.looks_like_request("ботяра, как дела?")
    assert picture_filename(JPEG) == "picture.jpg" and picture_filename(b"\x89PNG...") == "picture.png"


async def test_free_picture_waits_out_the_gap_and_retries_once(tmp_path: Path, storage: Storage) -> None:
    http, urls = pollinations(402, 200)
    clock = Clock()
    maker = ImageMaker(storage, FakeLLM(), http=http, clock=clock, sleep=clock.sleep, gap=16)  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage)
    assert await maker.draw("a cat", rt) == JPEG
    assert clock.slept == [16.0]  # the refusal, then the pause Pollinations wants
    assert "a%20cat" in urls[0] and "safe=true" in urls[0]

    queued: list[tuple[int, int]] = []

    async def on_queue(position: int, seconds: int) -> None:
        queued.append((position, seconds))

    assert await maker.draw("a dog", rt, on_queue=on_queue) == JPEG  # right after: has to wait its turn
    assert queued == [(1, 16)]


async def test_paid_fallback_only_within_the_daily_cap(tmp_path: Path, storage: Storage) -> None:
    clock = Clock()
    http, _ = pollinations(*[500] * 10)
    llm = PaidLLM()
    maker = ImageMaker(storage, llm, http=http, clock=clock, sleep=clock.sleep)  # type: ignore[arg-type]
    assert await maker.draw("a cat", await runtime(tmp_path, storage)) is None  # 0 paid a day: never pay
    assert llm.paid == []
    capped = await runtime(tmp_path, storage, paid_images_per_day=1)
    assert await maker.draw("a cat", capped) == b"\x89PNG paid"
    assert await maker.draw("a dog", capped) is None  # the one paid picture of the day is spent
    assert llm.paid == ["a cat"]


async def test_per_user_limit(tmp_path: Path, storage: Storage) -> None:
    maker = ImageMaker(storage, FakeLLM())  # type: ignore[arg-type]
    unlimited, one = await runtime(tmp_path, storage), await runtime(tmp_path, storage, images_per_user_per_day=1)
    assert await maker.left_today(IVAN, one)
    await maker.count_drawn(IVAN)
    assert not await maker.left_today(IVAN, one)
    assert await maker.left_today(IVAN, unlimited)
    await maker.aclose()


def make_responder(
    tmp_path: Path, storage: Storage, llm: FakeLLM, transport: FakeTransport, maker: ImageMaker
) -> Responder:
    settings = make_settings(tmp_path, **SLOW)
    formatter = LineFormatter(ZoneInfo(settings.timezone), settings.focus_users)
    config = BotConfig(storage, settings)
    return Responder(
        transport=transport,
        storage=storage,
        llm=llm,  # type: ignore[arg-type]
        context=ContextBuilder(storage, config, settings, BOT, formatter),
        settings=settings,
        bot_config=config,
        me=BOT,
        images=maker,
    )


async def test_a_drawing_request_gets_a_picture(tmp_path: Path, storage: Storage) -> None:
    http, _ = pollinations(200)
    llm, transport = FakeLLM(PLAN), FakeTransport()
    responder = make_responder(tmp_path, storage, llm, transport, ImageMaker(storage, llm, http=http))  # type: ignore[arg-type]
    await storage.add_message(msg(5, "ботяра, нарисуй кота в танке", user_id=IVAN, author="Иван"))
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)

    [sent] = transport.sent
    assert (sent.image, sent.text, sent.reply_to, sent.notify) == (JPEG, "Держи, танкист", 5, True)
    assert '"draw": true' in llm.prompt_text() and "К тебе обратились в сообщении #1" in llm.prompt_text()
    remembered = await storage.get_message(CHAT, sent.message_id)
    assert remembered is not None and remembered.text.startswith("[картинка: a cat driving a tank")
    await responder.shutdown()


async def test_not_a_drawing_request_is_answered_as_usual(tmp_path: Path, storage: Storage) -> None:
    llm, transport = FakeLLM('{"draw": false}', "Красивая, да."), FakeTransport()
    responder = make_responder(tmp_path, storage, llm, transport, ImageMaker(storage, llm))  # type: ignore[arg-type]
    await storage.add_message(msg(5, "ботяра, как тебе эта картинка?", user_id=IVAN, author="Иван"))
    responder.enqueue(CHAT, Incoming(5, IVAN, addressed=True, trivial=False))
    await responder.process(CHAT)
    assert [(s.text, getattr(s, "image", None)) for s in transport.sent] == [("Красивая, да.", None)]
    await responder.shutdown()


async def test_openrouter_image_models(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG!").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if payload["model"] == "broken/model":
            return httpx.Response(400, json={"error": {"message": "no"}})
        return httpx.Response(200, json={"choices": [{"message": {"images": [{"image_url": {"url": data_url}}]}}]})

    client = LLMClient(make_settings(tmp_path), http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.generate_image("a cat", models=["broken/model", "good/model"]) == b"\x89PNG!"
    assert [c["model"] for c in calls] == ["broken/model", "good/model"]
    assert calls[0]["modalities"] == ["image", "text"]


def artists(cloudflare: list[int], keyed: list[int], anonymous: list[int]) -> tuple[httpx.AsyncClient, list[str]]:
    """Cloudflare, keyed and anonymous Pollinations that answer these statuses in turn (200 = a picture)."""
    calls: list[str] = []
    queues = {"api.cloudflare.com": cloudflare, "gen.pollinations.ai": keyed, "image.pollinations.ai": anonymous}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host if host != "gen.pollinations.ai" else f"{host}:{request.url.params['model']}")
        status = queues[host].pop(0) if queues[host] else 200
        if host == "api.cloudflare.com":
            if status == 200:
                return httpx.Response(200, json={"success": True, "result": {"image": base64.b64encode(JPEG).decode()}})
            return httpx.Response(
                status, json={"success": False, "errors": [{"code": 4006, "message": "daily free allocation exceeded"}]}
            )
        if status == 200:
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(status, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls


async def test_cheapest_artist_first_and_cloudflare_rests_once_its_allocation_is_spent(
    tmp_path: Path, storage: Storage
) -> None:
    http, calls = artists(cloudflare=[200, 429], keyed=[402, 200], anonymous=[])
    clock = Clock()
    maker = ImageMaker(
        storage,
        FakeLLM(),  # type: ignore[arg-type]
        http=http,
        clock=clock,
        sleep=clock.sleep,
        pollinations_key="sk-test",
        cloudflare=("acc", "cf-token", "@cf/flux"),
    )
    rt = await runtime(tmp_path, storage)
    assert await maker.draw("a cat", rt) == JPEG
    assert calls == ["api.cloudflare.com"]
    calls.clear()
    assert await maker.draw("a dog", rt) == JPEG  # allocation spent: zimage out of pollen, flux draws
    assert calls == ["api.cloudflare.com", "gen.pollinations.ai:zimage", "gen.pollinations.ai:flux"]
    calls.clear()
    assert await maker.draw("a cow", rt) == JPEG  # Cloudflare is not asked again today
    assert calls == ["gen.pollinations.ai:zimage"]


async def test_without_keys_only_the_anonymous_sana(tmp_path: Path, storage: Storage) -> None:
    http, calls = artists(cloudflare=[], keyed=[], anonymous=[200])
    maker = ImageMaker(storage, FakeLLM(), http=http)  # type: ignore[arg-type]
    assert await maker.draw("a cat", await runtime(tmp_path, storage)) == JPEG
    assert calls == ["image.pollinations.ai"]
