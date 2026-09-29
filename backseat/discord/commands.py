"""Slash commands. The logic lives in plain functions over the interaction, so tests can pass a small
fake; register_commands only wraps them for discord.py.

/prompt and /status are owner-only: hidden from members without the Administrator permission, checked
again at runtime and always answered ephemerally — the persona names whom the bot roasts."""

import io
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from backseat import __version__
from backseat.bot_config import BotConfig, parse_names
from backseat.commands import decode_prompt_file, is_prompt_file, is_reset, status_text
from backseat.digest import WeeklyDigest
from backseat.discord.messages import channel_allowed
from backseat.discord.settings import DiscordSettings
from backseat.discord.transport import DiscordTransport
from backseat.llm import LLMClient, LLMError
from backseat.replies import split_message
from backseat.responder import Responder
from backseat.storage import Storage
from backseat.transport import Sent
from backseat.triggers import BotIdentity

log = logging.getLogger(__name__)

HELP_TEXT = """\
Я {username} v{version}: читаю канал, помню всю беседу и иногда вставляю пару слов.
Позвать меня: @{username}, ответ на моё сообщение или по имени ({names}). На обращение отвечаю всегда.

Команды:
/names — на какие имена откликаюсь
/digest — итоги недели прямо сейчас
/help — эта справка"""

OWNER_HELP = """

Только для владельца, ответы видишь только ты:
/prompt — показать мой характер. Поменять: text или файлом .txt/.md, вернуть по умолчанию: reset
/names с именами через запятую — поменять имена, «сброс» — вернуть по умолчанию
/status — модели, память по каналам, расходы и лимиты"""

NOT_OWNER = "Эта команда только для владельца бота."

_WEEK_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Services:
    settings: DiscordSettings
    storage: Storage
    bot_config: BotConfig
    llm: LLMClient
    responder: Responder
    digest: WeeklyDigest
    me: BotIdentity
    owners: frozenset[int]  # OWNER_IDS plus whoever owns the application
    clock: Callable[[], float] = time.monotonic
    last_digest: dict[int, float] = field(default_factory=dict)  # channel id -> clock() of its last /digest


def application_owners(info: discord.AppInfo) -> frozenset[int]:
    """Whoever owns the bot in the Developer Portal: its owner, or every member of the owning team."""
    if info.team is not None:
        return frozenset(member.id for member in info.team.members)
    return frozenset({info.owner.id})


async def _private(interaction: discord.Interaction, text: str, **kwargs: Any) -> None:
    await interaction.response.send_message(text, ephemeral=True, **kwargs)


async def _owner_only(svc: Services, interaction: discord.Interaction, denial: str = NOT_OWNER) -> bool:
    if interaction.user.id in svc.owners:
        return True
    await _private(interaction, denial)
    return False


async def cmd_help(svc: Services, interaction: discord.Interaction) -> None:
    names = ", ".join(await svc.bot_config.names())
    text = HELP_TEXT.format(version=__version__, username=svc.me.username, names=names)
    if interaction.user.id in svc.owners:
        text += OWNER_HELP
    await _private(interaction, text)


async def cmd_names(svc: Services, interaction: discord.Interaction, names: str | None) -> None:
    arg = (names or "").strip()
    if not arg:
        current = ", ".join(await svc.bot_config.names())
        await _private(interaction, f"Откликаюсь на: {current} и @{svc.me.username}")
        return
    if not await _owner_only(svc, interaction, "Менять имена может только владелец бота."):
        return
    if is_reset(arg):
        await svc.bot_config.set_names(None)
        current = ", ".join(await svc.bot_config.names())
        await _private(interaction, f"Вернул имена по умолчанию: {current}")
        return
    new_names = parse_names(arg)
    if not new_names:
        await _private(interaction, "Не понял имена. Пример: /names бэксит, ботяра")
        return
    await svc.bot_config.set_names(new_names)
    await _private(interaction, f"Теперь откликаюсь на: {', '.join(new_names)} и @{svc.me.username}")


async def cmd_status(svc: Services, interaction: discord.Interaction) -> None:
    if not await _owner_only(svc, interaction):
        return
    # Discord waits only 3 seconds for an answer, OpenRouter may take longer.
    await interaction.response.defer(ephemeral=True, thinking=True)
    allowed = (await svc.bot_config.runtime()).allowed_chat_ids
    chats = [(chat_id, f"Память <#{chat_id}>") for chat_id in allowed or [interaction.channel_id]]
    text = await status_text(
        svc.me.username,
        chats,
        storage=svc.storage,
        bot_config=svc.bot_config,
        llm=svc.llm,
        tz=ZoneInfo(svc.settings.timezone),
    )
    await interaction.followup.send(text, ephemeral=True)


async def read_text_attachment(attachment: discord.Attachment) -> str | None:
    if not is_prompt_file(attachment.content_type, attachment.filename, attachment.size):
        return None
    try:
        raw = await attachment.read()
    except discord.HTTPException as exc:
        log.warning("Could not download %s: %s", attachment.filename, exc)
        return None
    return decode_prompt_file(raw)


async def _show_persona(svc: Services, interaction: discord.Interaction) -> None:
    persona = await svc.bot_config.persona()
    title = "Свой характер" if await svc.bot_config.has_custom_persona() else "Характер по умолчанию"
    text = f"{title}:\n\n{persona}"
    if len(text) <= DiscordTransport.max_length:
        await _private(interaction, text)
    else:
        file = discord.File(io.BytesIO(persona.encode("utf-8")), filename="prompt.txt")
        await _private(interaction, f"{title} — в файле.", file=file)


async def cmd_prompt(
    svc: Services,
    interaction: discord.Interaction,
    text: str | None = None,
    file: discord.Attachment | None = None,
    reset: bool = False,
) -> None:
    if not await _owner_only(svc, interaction):
        return
    if reset or is_reset(text):
        await svc.bot_config.set_persona(None)
        await _private(interaction, "Вернул характер по умолчанию.")
        return
    if file is not None:
        persona = await read_text_attachment(file)
        if persona is None:
            await _private(interaction, "Не смог прочитать файл: нужен текстовый .txt или .md до 100 КБ.")
            return
    else:
        persona = (text or "").strip()
    if not persona:
        await _show_persona(svc, interaction)
        return
    await svc.bot_config.set_persona(persona)
    await _private(interaction, f"Новый характер сохранён: {len(persona)} символов.")


async def _say_privately_after_defer(interaction: discord.Interaction, text: str) -> None:
    """A public "thinking…" message can't become ephemeral: delete it, then tell the caller alone."""
    try:
        await interaction.delete_original_response()
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException as exc:
        log.warning("Could not answer user=%s privately: %s", interaction.user.id, exc)


async def cmd_digest(svc: Services, interaction: discord.Interaction) -> None:
    channel = interaction.channel
    if channel is None or not channel_allowed(channel, (await svc.bot_config.runtime()).allowed_chat_ids):
        await _private(interaction, "Здесь я итоги не подвожу.")
        return
    chat_id = channel.id
    if (await svc.bot_config.runtime()).paused:
        await _private(interaction, "Я сейчас на паузе: итоги подведу, когда меня включат.")
        return
    now = svc.clock()
    wait = svc.last_digest.get(chat_id, -math.inf) + svc.settings.digest_cooldown_seconds - now
    if wait > 0:
        await _private(interaction, f"Итоги недавно подводили, подожди {math.ceil(wait / 60)} мин.")
        return
    svc.last_digest[chat_id] = now  # also turns away a second /digest while this one is being written
    await interaction.response.defer(thinking=True)
    try:
        text = (await svc.digest.compose(chat_id, int(time.time()) - _WEEK_SECONDS) or "").strip()
    except LLMError as exc:
        log.warning("chat=%s /digest failed: %s", chat_id, exc)
        svc.last_digest.pop(chat_id, None)  # nothing was posted, so they may try again
        await _say_privately_after_defer(
            interaction, "Не получилось подвести итоги: модели не отвечают. Попробуй позже."
        )
        return
    if not text:
        svc.last_digest.pop(chat_id, None)
        await _say_privately_after_defer(interaction, "Подводить нечего: за неделю здесь почти ничего не писали.")
        return
    for chunk in split_message(text, DiscordTransport.max_length):
        message = await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none(), wait=True)
        # Remembered like any other message of the bot, so it knows what it has already said.
        await svc.responder.remember(chat_id, Sent(message.id, int(message.created_at.timestamp())), chunk)
    log.info("chat=%s digest posted on request of user=%s", chat_id, interaction.user.id)


def register_commands(tree: app_commands.CommandTree, svc: Services) -> None:
    """Guild-only commands; the caller syncs them globally."""

    @tree.command(name="help", description="Кто я, как меня позвать и что я умею")
    @app_commands.guild_only()
    async def help_(interaction: discord.Interaction) -> None:
        await cmd_help(svc, interaction)

    @tree.command(name="names", description="Имена, на которые я откликаюсь")
    @app_commands.guild_only()
    @app_commands.describe(names="Новые имена через запятую или «сброс» (только владелец)")
    async def names_(interaction: discord.Interaction, names: str | None = None) -> None:
        await cmd_names(svc, interaction, names)

    @tree.command(name="digest", description="Итоги недели в этом канале прямо сейчас")
    @app_commands.guild_only()
    async def digest_(interaction: discord.Interaction) -> None:
        await cmd_digest(svc, interaction)

    @tree.command(name="status", description="Модели, память и расходы (только владелец)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def status_(interaction: discord.Interaction) -> None:
        await cmd_status(svc, interaction)

    @tree.command(name="prompt", description="Мой характер: показать, поменять или сбросить (только владелец)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        text="Новый характер текстом",
        file="Новый характер файлом .txt или .md до 100 КБ",
        reset="Вернуть характер по умолчанию",
    )
    async def prompt_(
        interaction: discord.Interaction,
        text: str | None = None,
        file: discord.Attachment | None = None,
        reset: bool = False,
    ) -> None:
        await cmd_prompt(svc, interaction, text, file, reset)
