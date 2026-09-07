import asyncio
import json
import logging
import os

import httpx

log = logging.getLogger("backseat")


from config import _env


OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL_NAME = _env("MODEL_NAME", "thinkingmachines/inkling:free")
# Optional, only used for OpenRouter's own leaderboards — harmless to leave blank.
OPENROUTER_SITE_URL = _env("OPENROUTER_SITE_URL", "")
OPENROUTER_APP_NAME = _env("OPENROUTER_APP_NAME", "Backseat")

# Output token budget: decision JSON + a short comment. Reasoning models can
# spend part of this on internal thinking before writing the actual content,
# so keep some headroom (raise further if you see empty-content warnings).
MAX_TOKENS = int(_env("MAX_TOKENS", "300"))
REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT_SECONDS", "30"))
# Safety cap on simultaneous requests to the provider (OpenRouter doesn't publish
# a hard concurrency limit like GonkaGate did, but keeping this bounded is cheap
# insurance and keeps a single chat's batch calls from piling up).
MAX_CONCURRENCY = int(_env("OPENROUTER_MAX_CONCURRENCY", "2"))

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy singleton — reuses connection pool and HTTP/2 across requests."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, http2=True)
    return _client


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). We don't have the exact tokenizer
    for every OpenRouter-hosted model, so this only keeps input context within a
    sane, configurable budget (MAX_INPUT_TOKENS in bot.py)."""
    return max(1, len(text) // 4)


_SYSTEM_TEMPLATE = """\
Ты — участник группового чата, а не ассистент. Вот твоя личность и стиль общения:

{bot_prompt}

Тебе показывают две части истории:
CONTEXT — более старые сообщения, только для понимания разговора, персонажей и \
callback-шуток. Это НЕ повод отвечать сам по себе.
NEW — последняя пачка сообщений, единственное, на что можно отреагировать сейчас.

Формат каждой строки: msg_id|user_id|автор|текст
user_id — это Telegram ID пользователя, он позволяет точно определить, кто написал \
сообщение (даже если имя изменилось). Используй user_id для сопоставления с описанием \
участников в промпте (например, если в промпте указан ID 807998762 — ищи его в поле user_id).
Иногда: id_начала-id_конца|user_id|автор|текст|xN — значит N одинаковых сообщений подряд, \
считай это одной ситуацией, а не N поводами ответить.

Правила:
- Автор рядом с сообщением — это всегда источник истины; никогда не путай, кто что \
написал, и не приписывай реплику другому участнику.
- На весь NEW разрешён максимум один комментарий.
- Отвечай, когда: тебя прямо позвали (@упоминание) или ответили на твоё сообщение; \
тебе или в чат задали вопрос, на который есть что ответить; есть по-настоящему \
удачный повод для шутки, сарказма или callback'а; несколько сообщений вместе \
образуют смешную сцену.
- Не отвечай, когда: обычный разговор идёт нормально и без тебя; сообщения вроде \
"ок"/"ага"/"понял"; шутка получится слабой, натянутой или повторяющей то, что ты \
уже говорил недавно.
- В случае сомнения — respond=false.

Ответь СТРОГО в формате JSON, без пояснений, рассуждений и markdown-разметки:
{{"respond": true или false, "reply_to_message_id": <id из NEW или null>, "comment": "текст или null"}}

reply_to_message_id обязателен и должен быть одним из msg_id, реально присутствующих \
в NEW, если respond=true; иначе null. comment — обычно 1-2 предложения, в стиле \
обычного сообщения в чат, без кавычек и без упоминания того, что ты ИИ."""

_FORCED_HINT = (
    "\n\n(В этой пачке тебя явно позвали/упомянули или ответили на твоё сообщение — "
    "это почти всегда повод ответить.)"
)


async def decide(
    bot_prompt: str,
    context_text: str,
    new_text: str,
    forced: bool = False,
) -> dict | None:
    """
    Ask the model whether Backseat should jump into the conversation given
    CONTEXT (older history) and NEW (the batch of messages just collected).

    Returns a dict {"respond": bool, "reply_to_message_id": int | None,
    "comment": str | None}, or None on a request/parsing failure (caller should
    treat that as "stay silent").
    """
    system_prompt = _SYSTEM_TEMPLATE.format(bot_prompt=bot_prompt)
    user_content = f"CONTEXT:\n{context_text or '(пусто)'}\n\nNEW:\n{new_text}"
    if forced:
        user_content += _FORCED_HINT

    payload = {
        "model": MODEL_NAME,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = OPENROUTER_SITE_URL
    if OPENROUTER_APP_NAME:
        headers["X-Title"] = OPENROUTER_APP_NAME

    async with _semaphore:
        client = _get_client()
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions", json=payload, headers=headers
        )
        if resp.status_code >= 400:
            log.error(
                "OpenRouter HTTP %s for model=%s: %s",
                resp.status_code, MODEL_NAME, resp.text[:2000],
            )
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]
    message = choice.get("message", {})
    raw = message.get("content")
    finish_reason = choice.get("finish_reason")

    if not raw:
        # Common with "reasoning" models: they can burn the whole max_tokens
        # budget on internal reasoning (sometimes in message["reasoning"]) and
        # leave content empty/null, especially when finish_reason == "length".
        log.warning(
            "Model returned empty content (finish_reason=%s); consider raising "
            "MAX_TOKENS if this keeps happening. Skipping this batch.",
            finish_reason,
        )
        return None
    raw = raw.strip()

    # Models sometimes wrap JSON in ```json ... ``` fences despite instructions.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Model did not return valid JSON, ignoring. Raw: %r", raw)
        return None

    if not isinstance(parsed, dict) or "respond" not in parsed:
        log.warning("Model JSON missing expected fields, ignoring. Raw: %r", raw)
        return None

    return {
        "respond": bool(parsed.get("respond")),
        "reply_to_message_id": parsed.get("reply_to_message_id"),
        "comment": (parsed.get("comment") or "").strip() or None,
    }
