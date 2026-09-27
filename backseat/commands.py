"""Platform-independent parts of the chat commands: the reset words, a persona sent as a text file
and the owner's /status report."""

from collections.abc import Iterable
from datetime import datetime
from zoneinfo import ZoneInfo

from backseat import __version__
from backseat.bot_config import BotConfig
from backseat.llm import LLMClient
from backseat.storage import Storage

# "/prompt сброс", "/names reset": back to the defaults.
RESET_WORDS = frozenset({"сброс", "сбросить", "reset", "default"})
MAX_PROMPT_FILE_BYTES = 100_000


def is_reset(arg: str | None) -> bool:
    return (arg or "").strip().casefold() in RESET_WORDS


def is_prompt_file(content_type: str | None, filename: str | None, size: int | None) -> bool:
    """A persona sent as a file: text (.txt, .md or any text/* type) of at most 100 KB."""
    is_text = (content_type or "").startswith("text/") or (filename or "").lower().endswith((".txt", ".md"))
    return is_text and (size or 0) <= MAX_PROMPT_FILE_BYTES


def decode_prompt_file(raw: bytes) -> str | None:
    """The file's text: UTF-8 (with or without a BOM), else Windows-1251. None if it is empty."""
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding).strip() or None
        except UnicodeDecodeError:
            continue
    return None


async def status_text(
    title: str,
    chats: Iterable[tuple[int, str]],
    *,
    storage: Storage,
    bot_config: BotConfig,
    llm: LLMClient,
    tz: ZoneInfo,
) -> str:
    """The owner's /status: models, pause, the memory of each (chat id, label), names and spending."""
    runtime = await bot_config.runtime()  # .env values with the web panel's overrides
    lines = [f"{title} v{__version__}", "Модели по порядку: " + " → ".join(runtime.models)]
    if runtime.paused:
        lines.append("⏸ На паузе: читаю и запоминаю, но молчу (включается в веб-панели)")
    if llm.last_model:
        lines.append(f"Последний ответ дала: {llm.last_model}")
    for chat_id, label in chats:
        memory = f"{label}: {await storage.count_messages(chat_id)} сообщений"
        summary = await storage.get_summary(chat_id)
        if summary:
            memory += f", сводка обновлена {datetime.fromtimestamp(summary.updated_at, tz):%d.%m %H:%M}"
        else:
            memory += ", сводки пока нет"
        lines.append(memory)
    lines.append("Имена: " + ", ".join(await bot_config.names()))
    info = await llm.key_info()
    if info:
        free = info.get("free_model_daily_requests") or {}
        if free.get("limit") is not None:
            lines.append(f"Бесплатные запросы сегодня: {free.get('used', 0)} из {free['limit']}")
        if info.get("usage_daily") is not None:
            lines.append(f"Потрачено сегодня: ${info['usage_daily']:.4f}")
    return "\n".join(lines)
