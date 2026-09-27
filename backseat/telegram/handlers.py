"""aiogram handlers: commands and the stream of group messages."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, BufferedInputFile, Document, Message

from backseat import __version__
from backseat.bot_config import BotConfig, parse_names
from backseat.commands import decode_prompt_file, is_prompt_file, is_reset, status_text
from backseat.config import CoreSettings
from backseat.llm import LLMClient
from backseat.responder import Incoming, Responder
from backseat.storage import Storage, StoredMessage
from backseat.telegram.messages import describe_message, find_address, is_trivial
from backseat.triggers import BotIdentity

log = logging.getLogger(__name__)

# Telegram menu commands must be Latin; the Cyrillic aliases work when typed.
BOT_COMMANDS = [
    BotCommand(command="help", description="Что умеет бот"),
    BotCommand(command="names", description="Имена, на которые бот откликается (/имена)"),
    BotCommand(command="id", description="Узнать свой Telegram ID"),
    BotCommand(command="ping", description="Проверить, что бот жив"),
]
# Owner-only commands, shown only in the owner's private chat with the bot. For everyone else they
# don't exist: /prompt says who gets roasted, /status shows spending.
OWNER_COMMANDS = [
    BotCommand(command="prompt", description="Характер бота (/промпт)"),
    BotCommand(command="status", description="Модель, память и расходы"),
    *BOT_COMMANDS,
]

HELP_TEXT = """\
Я Бэксит v{version}: читаю чат, помню всю беседу и иногда вставляю пару слов.
Позвать меня: @{username}, ответ на моё сообщение или по имени ({names}). На обращение отвечаю всегда.

Команды:
/names или /имена — на какие имена откликаюсь. Владелец меняет: /имена бэксит, ботяра
/id — узнать свой Telegram ID
/ping — проверить, что я жив"""

OWNER_HELP = """

Только для тебя, здесь в личке:
/prompt или /промпт — показать мой характер. Поменять: /промпт новый текст, \
.txt-файлом с подписью /промпт или /промпт сброс
/status — модели, память по чатам, расходы и лимиты"""

_GROUPS = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
_INLINE_PROMPT_LIMIT = 3500


@dataclass(frozen=True, slots=True)
class Services:
    settings: CoreSettings
    storage: Storage
    bot_config: BotConfig
    llm: LLMClient
    responder: Responder
    me: BotIdentity


async def register_commands(bot: Bot, owner_ids: Iterable[int]) -> None:
    """Everyone gets the public menu; owners also see /prompt in their private chat with the bot."""
    await bot.set_my_commands(BOT_COMMANDS)
    for owner_id in owner_ids:
        try:
            await bot.set_my_commands(OWNER_COMMANDS, scope=BotCommandScopeChat(chat_id=owner_id))
        except TelegramAPIError as exc:
            log.warning("No owner menu for %s — they must /start the bot in private first: %s", owner_id, exc)


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
    if not is_prompt_file(document.mime_type, document.file_name, document.file_size):
        return None
    buffer = await bot.download(document)
    return decode_prompt_file(buffer.read()) if buffer is not None else None


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
        names = ", ".join(await svc.bot_config.names())
        text = HELP_TEXT.format(version=__version__, username=svc.me.username, names=names)
        if message.chat.type == ChatType.PRIVATE and is_owner(message):
            text += OWNER_HELP
        await message.reply(text)

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

    async def chat_title(bot: Bot, chat_id: int) -> str:
        try:
            chat = await bot.get_chat(chat_id)
        except TelegramAPIError:
            return str(chat_id)
        return f"«{chat.title}»" if chat.title else str(chat_id)

    @router.message(Command("status", "статус", ignore_case=True))
    async def cmd_status(message: Message, bot: Bot) -> None:
        if not is_owner(message):
            return  # for everyone else the command does not exist
        if message.chat.type == ChatType.PRIVATE:
            # From the private chat, report on the group chats the bot lives in.
            chat_ids = svc.settings.allowed_chat_ids or await svc.storage.active_chats(0)
            chats = [(chat_id, f"Память чата {await chat_title(bot, chat_id)}") for chat_id in chat_ids]
        else:
            chats = [(message.chat.id, "Память этого чата")]
        text = await status_text("Бэксит", chats, storage=svc.storage, bot_config=svc.bot_config, llm=svc.llm, tz=tz)
        await message.reply(text)

    async def show_persona(message: Message) -> None:
        persona = await svc.bot_config.persona()
        title = "Свой характер" if await svc.bot_config.has_custom_persona() else "Характер по умолчанию"
        if len(persona) <= _INLINE_PROMPT_LIMIT:
            await message.reply(f"{title}:\n\n{persona}")
        else:
            await message.reply_document(
                BufferedInputFile(persona.encode("utf-8"), filename="prompt.txt"), caption=title
            )

    @router.message(Command("prompt", "промпт", ignore_case=True))
    async def cmd_prompt(message: Message, command: CommandObject, bot: Bot) -> None:
        if not is_owner(message):
            return  # for everyone else the command does not exist
        if message.chat.type != ChatType.PRIVATE:
            await message.reply("Промпт настраивается только у меня в личке.")
            return
        arg = (command.args or "").strip()
        if not arg and not message.document:
            await show_persona(message)
            return
        if message.document:
            text = await read_text_file(bot, message.document)
            if text is None:
                await message.reply("Не смог прочитать файл: нужен текстовый .txt или .md до 100 КБ.")
                return
        elif is_reset(arg):
            await svc.bot_config.set_persona(None)
            await message.reply("Вернул характер по умолчанию.")
            return
        else:
            text = arg
        await svc.bot_config.set_persona(text)
        await message.reply(f"Новый характер сохранён: {len(text)} символов.")

    @router.message(Command("names", "имена", ignore_case=True))
    async def cmd_names(message: Message, command: CommandObject) -> None:
        if not chat_allowed(message):
            return
        arg = (command.args or "").strip()
        if not arg:
            names = ", ".join(await svc.bot_config.names())
            await message.reply(f"Откликаюсь на: {names} и @{svc.me.username}")
            return
        if not is_owner(message):
            await deny(message)
            return
        if is_reset(arg):
            await svc.bot_config.set_names(None)
            names = ", ".join(await svc.bot_config.names())
            await message.reply(f"Вернул имена по умолчанию: {names}")
            return
        new_names = parse_names(arg)
        if not new_names:
            await message.reply("Не понял имена. Пример: /имена бэксит, ботяра")
            return
        await svc.bot_config.set_names(new_names)
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
        # Every @mention, name call and reply to the bot gets an answer — even a bare sticker reply.
        addressed = find_address(message, svc.me, await svc.bot_config.name_pattern()) is not None
        svc.responder.enqueue(message.chat.id, Incoming(message.message_id, user_id, addressed, is_trivial(message)))

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
