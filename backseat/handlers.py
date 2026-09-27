"""aiogram handlers: commands and the stream of group messages."""

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BufferedInputFile, Document, Message

from backseat import __version__
from backseat.chat_config import ChatConfig
from backseat.config import Settings
from backseat.llm import LLMClient
from backseat.render import describe_message
from backseat.responder import Incoming, Responder
from backseat.storage import Storage, StoredMessage
from backseat.triggers import Address, BotIdentity, find_address, is_trivial

# Telegram menu commands must be Latin; the Cyrillic aliases work when typed.
BOT_COMMANDS = [
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="prompt", description="Характер бота (/промпт)"),
    BotCommand(command="names", description="Имена, на которые бот откликается (/имена)"),
    BotCommand(command="status", description="Модель, память и лимиты"),
    BotCommand(command="id", description="Узнать свой Telegram ID"),
    BotCommand(command="ping", description="Проверить, что бот жив"),
]

HELP_TEXT = """\
Я Бэксит v{version}: читаю чат, помню всю беседу и иногда вставляю пару слов.
Позвать меня: @{username}, ответ на моё сообщение или по имени ({names}). На обращение отвечаю всегда.

Команды:
/prompt или /промпт — показать мой характер. Владелец меняет его: /промпт новый текст, \
.txt-файлом с подписью /промпт или /промпт сброс
/names или /имена — на какие имена откликаюсь. Владелец меняет: /имена бэксит, ботяра
/status — модель, память и лимиты
/id — узнать свой Telegram ID
/ping — проверить, что я жив"""

_GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
_RESET_WORDS = {"сброс", "сбросить", "reset", "default"}
_MAX_PROMPT_FILE_BYTES = 100_000
_INLINE_PROMPT_LIMIT = 3500


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    storage: Storage
    chat_config: ChatConfig
    llm: LLMClient
    responder: Responder
    me: BotIdentity


def sender_of(message: Message) -> tuple[int, str, bool]:
    """(id, display name, is another bot) — anonymous admins and channel posts included."""
    chat = message.sender_chat
    if chat is not None:
        if chat.id == message.chat.id:  # an admin posting anonymously as the group
            return chat.id, message.author_signature or "Анонимный админ", False
        return chat.id, chat.title or "Канал", False
    user = message.from_user
    if user is None:
        return 0, "Неизвестный", False
    return user.id, user.full_name, user.is_bot


async def read_text_file(bot: Bot, document: Document) -> str | None:
    is_text = (document.mime_type or "").startswith("text/") or (document.file_name or "").lower().endswith(
        (".txt", ".md")
    )
    if not is_text or (document.file_size or 0) > _MAX_PROMPT_FILE_BYTES:
        return None
    buffer = await bot.download(document)
    if buffer is None:
        return None
    raw = buffer.read()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding).strip() or None
        except UnicodeDecodeError:
            continue
    return None


def create_router(svc: Services) -> Router:
    router = Router(name="backseat")
    tz = ZoneInfo(svc.settings.timezone)

    def chat_allowed(message: Message) -> bool:
        allowed = svc.settings.allowed_chat_ids
        return message.chat.type == ChatType.PRIVATE or not allowed or message.chat.id in allowed

    def is_owner(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in svc.settings.owner_ids

    async def deny(message: Message) -> None:
        if svc.settings.owner_ids:
            await message.reply("Это может менять только владелец бота.")
        else:
            await message.reply("Владелец не настроен: впишите свой ID (команда /id) в OWNER_IDS на сервере.")

    @router.message(CommandStart())
    @router.message(Command("help", "помощь", ignore_case=True))
    async def cmd_help(message: Message) -> None:
        names = ", ".join(await svc.chat_config.names(message.chat.id))
        await message.reply(HELP_TEXT.format(version=__version__, username=svc.me.username, names=names))

    @router.message(Command("ping", "пинг", ignore_case=True))
    async def cmd_ping(message: Message) -> None:
        await message.reply("pong")

    @router.message(Command("id", "айди", ignore_case=True))
    async def cmd_id(message: Message) -> None:
        lines = []
        if message.from_user:
            lines.append(f"Твой Telegram ID: {message.from_user.id}")
        if message.chat.type != ChatType.PRIVATE:
            lines.append(f"ID этого чата: {message.chat.id}")
        await message.reply("\n".join(lines) or "Не вижу, кто ты.")

    @router.message(Command("status", "статус", ignore_case=True))
    async def cmd_status(message: Message) -> None:
        if not chat_allowed(message):
            return
        chat_id = message.chat.id
        lines = [f"Бэксит v{__version__}", "Модели по порядку: " + " → ".join(svc.llm.models)]
        if svc.llm.last_model:
            lines.append(f"Последний ответ дала: {svc.llm.last_model}")
        memory = f"Память чата: {await svc.storage.count_messages(chat_id)} сообщений"
        summary = await svc.storage.get_summary(chat_id)
        if summary:
            memory += f", сводка обновлена {datetime.fromtimestamp(summary.updated_at, tz):%d.%m %H:%M}"
        else:
            memory += ", сводки пока нет"
        lines.append(memory)
        persona = "свой для этого чата" if await svc.chat_config.has_custom_persona(chat_id) else "по умолчанию"
        names = ", ".join(await svc.chat_config.names(chat_id))
        lines.append(f"Характер: {persona}; имена: {names}")
        info = await svc.llm.key_info()
        if info:
            free = info.get("free_model_daily_requests") or {}
            if free.get("limit") is not None:
                lines.append(f"Бесплатные запросы сегодня: {free.get('used', 0)} из {free['limit']}")
            if info.get("usage_daily") is not None:
                lines.append(f"Потрачено сегодня: ${info['usage_daily']:.4f}")
        await message.reply("\n".join(lines))

    async def show_persona(message: Message) -> None:
        chat_id = message.chat.id
        persona = await svc.chat_config.persona(chat_id)
        custom = await svc.chat_config.has_custom_persona(chat_id)
        title = "Характер для этого чата" if custom else "Характер по умолчанию"
        if len(persona) <= _INLINE_PROMPT_LIMIT:
            await message.reply(f"{title}:\n\n{persona}")
        else:
            await message.reply_document(
                BufferedInputFile(persona.encode("utf-8"), filename="prompt.txt"), caption=title
            )

    @router.message(Command("prompt", "промпт", ignore_case=True))
    async def cmd_prompt(message: Message, command: CommandObject, bot: Bot) -> None:
        if not chat_allowed(message):
            return
        chat_id = message.chat.id
        arg = (command.args or "").strip()
        if not arg and not message.document:
            await show_persona(message)
            return
        if not is_owner(message):
            await deny(message)
            return
        if message.document:
            text = await read_text_file(bot, message.document)
            if text is None:
                await message.reply("Не смог прочитать файл: нужен текстовый .txt или .md до 100 КБ.")
                return
        elif arg.casefold() in _RESET_WORDS:
            await svc.chat_config.set_persona(chat_id, None)
            await message.reply("Вернул характер по умолчанию.")
            return
        else:
            text = arg
        await svc.chat_config.set_persona(chat_id, text)
        await message.reply(f"Новый характер сохранён: {len(text)} символов.")

    @router.message(Command("names", "имена", ignore_case=True))
    async def cmd_names(message: Message, command: CommandObject) -> None:
        if not chat_allowed(message):
            return
        chat_id = message.chat.id
        arg = (command.args or "").strip()
        if not arg:
            names = ", ".join(await svc.chat_config.names(chat_id))
            await message.reply(f"Откликаюсь на: {names} и @{svc.me.username}")
            return
        if not is_owner(message):
            await deny(message)
            return
        if arg.casefold() in _RESET_WORDS:
            await svc.chat_config.set_names(chat_id, None)
            names = ", ".join(await svc.chat_config.names(chat_id))
            await message.reply(f"Вернул имена по умолчанию: {names}")
            return
        new_names = list(dict.fromkeys(name.strip() for name in re.split(r"[,;\n]+", arg) if name.strip()))
        if not new_names:
            await message.reply("Не понял имена. Пример: /имена бэксит, ботяра")
            return
        await svc.chat_config.set_names(chat_id, new_names)
        await message.reply(f"Теперь откликаюсь на: {', '.join(new_names)} и @{svc.me.username}")

    @router.message(_GROUPS)
    async def on_group_message(message: Message) -> None:
        if not chat_allowed(message):
            return
        text = describe_message(message)
        if not text:
            return
        user_id, author, is_other_bot = sender_of(message)
        await svc.storage.add_message(
            StoredMessage(
                chat_id=message.chat.id,
                message_id=message.message_id,
                user_id=user_id,
                author=author,
                text=text,
                reply_to=message.reply_to_message.message_id if message.reply_to_message else None,
                is_bot=False,
                created_at=int(message.date.timestamp()),
            )
        )
        if is_other_bot:
            return  # remember other bots' messages, never talk to them
        trivial = is_trivial(message)
        address = find_address(message, svc.me, await svc.chat_config.name_pattern(message.chat.id))
        # A bare "😂" in reply to the bot is a reaction, not a question: let the model decide.
        addressed = address in (Address.MENTION, Address.NAME) or (address is Address.REPLY and not trivial)
        svc.responder.enqueue(message.chat.id, Incoming(message.message_id, user_id, addressed, trivial))

    @router.edited_message(_GROUPS)
    async def on_group_edit(message: Message) -> None:
        if not chat_allowed(message):
            return
        text = describe_message(message)
        if text:
            edited_at = message.edit_date or int(message.date.timestamp())  # edit_date is unix seconds
            await svc.storage.edit_message(message.chat.id, message.message_id, text, edited_at)

    @router.message(F.chat.type == ChatType.PRIVATE)
    async def on_private(message: Message) -> None:
        await message.reply("Я живу в групповом чате, в личке не болтаю. Команды: /help")

    return router
