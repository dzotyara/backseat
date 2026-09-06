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
MAX_TOKENS = int(_env("MAX_TOKENS", "200"))
REQUEST_TIMEOUT = float(_env("REQUEST_TIMEOUT_SECONDS", "30"))
# GonkaGate enforces a concurrent-requests limit ("too many concurrent requests"),
# separate from any per-minute rate limit. Keep this at or below your plan's limit.
GONKAGATE_MAX_CONCURRENCY = int(_env("GONKAGATE_MAX_CONCURRENCY", "1"))

_semaphore = asyncio.Semaphore(GONKAGATE_MAX_CONCURRENCY)

_DECISION_SYSTEM_PROMPT_TEMPLATE = """\
Ты — участник группового чата по имени "Backseat". Твоя роль/характер задаётся \
следующей инструкцией: 

{bot_prompt}

Тебе дана последняя история сообщений чата (могут быть подписаны именами \
участников). Реши, стоит ли тебе сейчас встрять с комментарием, следуя своей \
роли выше, или лучше промолчать (не нужно комментировать каждое сообщение — \
только когда это уместно и добавляет что-то).

Если сообщение адресовано тебе напрямую (упоминание, реплай, вопрос к тебе) — \
почти всегда стоит ответить.

Ответь СТРОГО в формате JSON без каких-либо пояснений и без markdown-разметки:
{{"should_comment": true или false, "comment": "текст комментария или пустая строка"}}

comment должен быть коротким (1-2 предложения), в стиле обычного сообщения в чат, \
без кавычек и без упоминания того, что ты ИИ или что ты анализируешь чат.
"""


async def decide_and_comment(bot_prompt: str, context: str, forced: bool = False) -> str | None:
    """
    Send the recent chat context to the model and let it decide whether to
    comment (following bot_prompt) and, if so, what to say.

    Returns the comment text, or None if the model decided to stay silent
    (or on error).
    """
    system_prompt = _DECISION_SYSTEM_PROMPT_TEMPLATE.format(bot_prompt=bot_prompt)
    user_content = context
    if forced:
        user_content += (
            "\n\n(Тебя явно позвали/упомянули в последнем сообщении — "
            "почти наверняка стоит ответить.)"
        )

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
