import asyncio
import json
import logging
import os

import httpx

log = logging.getLogger("backseat")


def _env(name: str, default: str) -> str:
    """Like os.environ.get, but treats an empty string as 'not set' too
    (env_file entries like FOO= otherwise override real defaults with '')."""
    value = os.environ.get(name)
    return value if value else default


GONKAGATE_API_KEY = os.environ["GONKAGATE_API_KEY"]
# GonkaGate is assumed OpenAI-compatible. Override GONKAGATE_BASE_URL if the real endpoint differs.
GONKAGATE_BASE_URL = _env("GONKAGATE_BASE_URL", "https://api.gonkagate.com/v1")
MODEL_NAME = _env("MODEL_NAME", "deepseek-ai/deepseek-v4-flash-0731")
# Output token budget (the model's JSON decision + comment).
MAX_TOKENS = int(_env("MAX_TOKENS", "200"))
REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT_SECONDS", "30"))
# GonkaGate enforces a concurrent-requests limit ("too many concurrent requests"),
# separate from any per-minute rate limit. Keep this at or below your plan's limit.
GONKAGATE_MAX_CONCURRENCY = int(_env("GONKAGATE_MAX_CONCURRENCY", "1"))

_semaphore = asyncio.Semaphore(GONKAGATE_MAX_CONCURRENCY)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token). We don't have GonkaGate/DeepSeek's
    exact tokenizer, so this is only used to keep the input context within a
    sane, configurable budget (MAX_INPUT_TOKENS in bot.py)."""
    return max(1, len(text) // 4)


_DECISION_SYSTEM_PROMPT_TEMPLATE = """\
Ты — участник группового чата по имени "Backseat". Твоя роль/характер задаётся \
следующей инструкцией:

{bot_prompt}

Тебе дана последняя история сообщений чата. Тебя спросили сейчас по одной из причин:
- тебя явно упомянули или ответили на твоё сообщение;
- в последнем сообщении явно задан вопрос;
- либо просто прошло достаточно сообщений с твоего последнего комментария, и сейчас
  плановый момент, когда можно (но не обязательно) встрять.

Приоритеты:
- Если тебя упомянули или тебе ответили — почти всегда стоит ответить.
- Если в последнем сообщении явно задан вопрос (необязательно тебе) и тебе есть что \
полезное или уместное сказать в своём характере — стоит ответить на него.
- Если повод — просто "подошла очередь" (планово), комментируй, только если реально \
есть что сказать уместное по своему характеру; если нет — смело отвечай \
should_comment=false, не нужно писать что-то через силу.

Ответь СТРОГО в формате JSON без каких-либо пояснений и без markdown-разметки:
{{"should_comment": true или false, "comment": "текст комментария или пустая строка"}}

comment должен быть коротким (1-2 предложения), в стиле обычного сообщения в чат, \
без кавычек и без упоминания того, что ты ИИ или что ты анализируешь чат.
"""

_REASON_HINTS = {
    "mention_or_reply": (
        "(Тебя явно позвали/упомянули или ответили на твоё сообщение — "
        "почти наверняка стоит ответить.)"
    ),
    "question": (
        "(В последнем сообщении, похоже, задан вопрос — если можешь полезно "
        "ответить на него в своём характере, стоит это сделать.)"
    ),
    "scheduled": (
        "(Прошло достаточно сообщений с последнего раза — сейчас нормальный момент, "
        "чтобы либо вставить комментарий, либо смолчать, если сказать нечего.)"
    ),
}


async def decide_and_comment(
    bot_prompt: str,
    context: str,
    forced: bool = False,
    trigger_reason: str = "scheduled",
) -> str | None:
    """
    Send the recent chat context to the model and let it decide whether to
    comment (following bot_prompt) and, if so, what to say.

    Returns the comment text, or None if the model decided to stay silent
    (or on error).
    """
    system_prompt = _DECISION_SYSTEM_PROMPT_TEMPLATE.format(bot_prompt=bot_prompt)
    reason_hint = _REASON_HINTS.get(
        "mention_or_reply" if forced else trigger_reason, _REASON_HINTS["scheduled"]
    )
    user_content = f"{context}\n\n{reason_hint}"

    payload = {
        "model": MODEL_NAME,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {GONKAGATE_API_KEY}",
        "Content-Type": "application/json",
    }

    async with _semaphore:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{GONKAGATE_BASE_URL}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            data = resp.json()

    raw = data["choices"][0]["message"]["content"].strip()

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

    if not parsed.get("should_comment"):
        return None

    comment = (parsed.get("comment") or "").strip()
    return comment or None
