import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from backseat.config import Settings
from backseat.llm import LLMClient, LLMError
from tests.conftest import make_settings

PROMPT = [{"role": "user", "content": "привет"}]


def make_client(
    settings: Settings,
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Callable[[], float] = lambda: 0.0,
) -> LLMClient:
    return LLMClient(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=clock)


def ok(text: str, model: str = "paid/model") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0001},
        },
    )


def model_of(request: httpx.Request) -> str:
    return json.loads(request.content)["model"]


async def test_first_model_answers(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("<think>так, подумаем</think>Привет")

    client = make_client(settings, handler)
    completion = await client.complete(PROMPT)
    assert completion.text == "Привет"
    assert completion.cost == 0.0001
    assert client.last_model == "paid/model"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "paid/model"
    assert payload["reasoning"] == {"enabled": False}
    assert payload["max_tokens"] == settings.max_tokens
    assert requests[0].headers["Authorization"] == "Bearer sk-test"
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"


async def test_no_credits_falls_back_and_cools_down(settings: Settings) -> None:
    now = [0.0]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(model_of(request))
        if model_of(request) == "paid/model":
            return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
        return ok("ответ", model_of(request))

    client = make_client(settings, handler, clock=lambda: now[0])
    assert (await client.complete(PROMPT)).model == "free/model:free"
    assert calls == ["paid/model", "free/model:free"]

    calls.clear()
    await client.complete(PROMPT)
    assert calls == ["free/model:free"]  # the paid model is skipped while cooling down

    now[0] += 601
    calls.clear()
    await client.complete(PROMPT)
    assert calls == ["paid/model", "free/model:free"]


async def test_error_body_empty_content_and_network_errors_fall_back(settings: Settings) -> None:
    for failure in (
        httpx.Response(200, json={"error": {"code": 502, "message": "upstream"}}),
        ok(""),
        ok("<think>только размышления</think>"),
        httpx.ConnectTimeout("slow"),
    ):

        def handler(request: httpx.Request, failure: object = failure) -> httpx.Response:
            if model_of(request) == "paid/model":
                if isinstance(failure, Exception):
                    raise failure
                return failure  # type: ignore[return-value]
            return ok("запасной", model_of(request))

        assert (await make_client(settings, handler).complete(PROMPT)).text == "запасной"


async def test_all_models_failing_raises_with_every_reason(settings: Settings) -> None:
    client = make_client(settings, lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(LLMError) as error:
        await client.complete(PROMPT)
    assert "paid/model" in str(error.value)
    assert "free/model:free" in str(error.value)


async def test_everything_cooling_down_is_still_tried(settings: Settings) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(model_of(request))
        return httpx.Response(429, text="slow down")

    client = make_client(settings, handler)
    with pytest.raises(LLMError):
        await client.complete(PROMPT)
    with pytest.raises(LLMError):
        await client.complete(PROMPT)
    assert calls == ["paid/model", "free/model:free"] * 2


async def test_reasoning_effort_and_limits_are_passed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, reasoning="low")
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return ok("ок")

    await make_client(settings, handler).complete(PROMPT, max_tokens=77, temperature=0.2)
    assert payloads[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert payloads[0]["max_tokens"] == 77
    assert payloads[0]["temperature"] == 0.2


async def test_key_info(settings: Settings) -> None:
    data = {"free_model_daily_requests": {"used": 5, "limit": 50}}
    client = make_client(settings, lambda request: httpx.Response(200, json={"data": data}))
    assert await client.key_info() == data
    broken = make_client(settings, lambda request: httpx.Response(401, text="nope"))
    assert await broken.key_info() is None
