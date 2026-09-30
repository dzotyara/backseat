"""Moderators ask the bot in plain words — «поменяй Ивокси ник на Антон, роль Морпех, цвет хаки»,
«замуть Васю на 10 минут», «забань Васю» — and it renames members, manages roles, mutes, kicks and bans.
Only MODERATOR_IDS (web panel); the model turns the request into JSON actions, the code checks and runs
them. A ban or a kick waits for the moderator's «да»; moderators, the owner and the bot are off limits."""

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import discord

from backseat.bot_config import Runtime
from backseat.llm import LLMClient, LLMError
from backseat.storage import ModerationEntry, Storage

log = logging.getLogger(__name__)

# Only messages that look like such a request cost a parsing call; the rest go to the usual answer.
REQUEST_RE = re.compile(
    r"ник|роль|роли|рол[ьюие]|цвет|переимен|назов|бан|кик|выгон|мут|замут|размут|тайм|nick|role|colou?r|ban|kick|mute",
    re.IGNORECASE,
)
_YES_RE = re.compile(r"\W*(да|ага|угу|давай|подтверждаю|конечно|го|yes|y)(?!\w)", re.IGNORECASE)
_NO_RE = re.compile(r"\W*(нет|не надо|отмена|отмени|стоп|no|n)(?!\w)", re.IGNORECASE)
CONFIRM = {"ban", "kick"}  # irreversible enough to ask first
CONFIRM_SECONDS = 120
MAX_MUTE_MINUTES = 28 * 24 * 60  # Discord's limit for a timeout
_HEX_RE = re.compile(r"#?([0-9a-fA-F]{6})")
_DIRECTORY_DAYS = 60
_MAX_MEMBERS = 150

PARSE_SYSTEM = """\
Ты разбираешь просьбу модератора Discord-сервера (ники, роли, муты, кики, баны) и переводишь её в JSON. \
Ничего не выдумывай: только то, о чём просят.

Просит: {requester} — «мне», «себе», «меня» значит он.{replied}
Участники (id — как их зовут в чате):
{members}

Роли сервера: {roles}

Ответь только JSON без пояснений:
{{"actions": [ ... ], "question": null}}
Действия:
{{"do": "nick", "user": <id>, "nick": "новый ник"}} — сменить ник на сервере; "nick": null — вернуть обычное имя.
{{"do": "give_role", "user": <id>, "role": "Название", "color": "#RRGGBB" или null}} — выдать роль; \
если такой нет, она будет создана; color — если просят цвет.
{{"do": "take_role", "user": <id>, "role": "Название"}} — забрать роль.
{{"do": "create_role", "role": "Название", "color": "#RRGGBB" или null}} — создать роль, никому не выдавая.
{{"do": "color", "role": "Название", "color": "#RRGGBB"}} — перекрасить роль.
{{"do": "mute", "user": <id>, "minutes": 10}} — тайм-аут: не может писать столько минут (по умолчанию 10; \
«на час» — 60, «на день» — 1440).
{{"do": "unmute", "user": <id>}} — снять тайм-аут.
{{"do": "kick", "user": <id>}} — выгнать с сервера.
{{"do": "ban", "user": <id>}} — забанить.
{{"do": "unban", "user": <id>}} — разбанить.
Людей сопоставляй по списку участников: ники бывают латиницей, а в просьбе — кириллицей \
(«Ивокси» — это IVOXY). Цвета переводи в HEX: хаки — #78866B, красный — #E53935 и так далее.
Если это вообще не такая просьба — {{"actions": [], "question": null}}.
Если непонятно, о ком или о чём речь, — {{"actions": [], "question": "короткий уточняющий вопрос"}}."""


@dataclass(frozen=True, slots=True)
class Plan:
    actions: list[dict[str, Any]]
    question: str | None


def parse_plan(text: str) -> Plan:
    """The model's JSON; anything unreadable is "not a request"."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    actions = [a for a in data.get("actions") or [] if isinstance(a, dict) and isinstance(a.get("do"), str)]
    question = data.get("question")
    return Plan(actions, question if isinstance(question, str) and question.strip() else None)


def parse_color(value: object) -> discord.Colour | None:
    if not isinstance(value, str):
        return None
    match = _HEX_RE.fullmatch(value.strip())
    return discord.Colour(int(match.group(1), 16)) if match else None


@dataclass(frozen=True, slots=True)
class _Pending:
    request: str  # for the moderation log
    actions: list[dict[str, Any]]
    names: dict[int, str]
    until: float


@dataclass(frozen=True, slots=True)
class _Asked:
    request: str  # the moderator's request the bot answered with a question
    until: float


class Moderation:
    def __init__(self, storage: Storage, llm: LLMClient, clock: Any = time.monotonic) -> None:
        self._storage = storage
        self._llm = llm
        self._clock = clock
        self._pending: dict[tuple[int, int], _Pending] = {}  # (channel, moderator) -> a ban/kick awaiting «да»
        self._asked: dict[tuple[int, int], _Asked] = {}  # (channel, moderator) -> a request awaiting an answer

    def awaits_answer(self, message: discord.Message) -> bool:
        """The moderator owes the bot an answer: «да» to a ban or kick, or who/what it asked about."""
        key = (message.channel.id, message.author.id)
        waiting = self._pending.get(key) or self._asked.get(key)
        return waiting is not None and waiting.until > self._clock()

    def wants(self, message: discord.Message, runtime: Runtime) -> bool:
        """A moderator's message that looks like such a request, or the answer to «точно?»."""
        if message.author.id not in runtime.moderator_ids:
            return False
        return self.awaits_answer(message) or bool(REQUEST_RE.search(message.content))

    async def handle(self, message: discord.Message, runtime: Runtime) -> str | None:
        """The report to post, or None when it was no such request after all (answer as usual)."""
        guild = message.guild
        assert guild is not None
        reason = f"по просьбе {message.author.display_name}"
        protected = {*runtime.moderator_ids, guild.owner_id, message.author.id, guild.me.id}
        key = (message.channel.id, message.author.id)
        pending = self._pending.pop(key, None)
        if pending is not None and pending.until > self._clock():
            if _YES_RE.match(message.content):
                return await self._run_all(message, pending.request, pending.actions, reason, protected)
            if _NO_RE.match(message.content):
                what = "; ".join(_describe_plan(action, pending.names) for action in pending.actions)
                await self._journal(message, pending.request, f"❌ отменено: {what}")
                return "Ок, отменил."
            # Anything else: the confirmation is dropped and the message is read as a new request.
        text = message.clean_content
        asked = self._asked.pop(key, None)
        if asked is not None and asked.until > self._clock():
            # «Кому выдать роль?» — «Умер в таркове»: the answer only makes sense with the request.
            text = f"{asked.request}\nУточнение на твой вопрос: {text}"
        try:
            plan = await self._plan(message, guild, runtime, text)
        except LLMError as exc:
            log.warning("moderation request %s not parsed: %s", message.id, exc)
            return "Не смог разобрать просьбу: нейросеть сейчас недоступна. Попробуй через минуту."
        if plan.question:
            self._asked[key] = _Asked(text, self._clock() + CONFIRM_SECONDS)
            return plan.question
        if not plan.actions:
            return None
        risky = [action for action in plan.actions if action["do"] in CONFIRM]
        if not risky:
            return await self._run_all(message, text, plan.actions, reason, protected)
        names = await self._directory(message, guild)
        self._pending[key] = _Pending(text, plan.actions, names, self._clock() + CONFIRM_SECONDS)
        what = "; ".join(_describe_plan(action, names) for action in plan.actions)
        return f"Точно? {what}. Ответь «да» в течение {CONFIRM_SECONDS // 60} минут — или «нет»."

    async def _run_all(
        self, message: discord.Message, request: str, actions: list[dict[str, Any]], reason: str, protected: set[int]
    ) -> str:
        guild = message.guild
        assert guild is not None
        lines = [await self._run(guild, action, reason, protected) for action in actions]
        log.info("moderation by user=%s: %s", message.author.id, " | ".join(lines))
        report = "\n".join(lines)
        await self._journal(message, request, report)
        return report

    async def _journal(self, message: discord.Message, request: str, result: str) -> None:
        """The panel's moderation log. A failed write must not undo the report."""
        entry = ModerationEntry(
            at=int(time.time()),
            chat_id=message.channel.id,
            moderator_id=message.author.id,
            moderator=_names(message.author),
            request=request,
            result=result,
        )
        try:
            await self._storage.add_moderation(entry)
        except Exception:
            log.warning("Could not write the moderation log", exc_info=True)

    async def _plan(self, message: discord.Message, guild: discord.Guild, runtime: Runtime, text: str) -> Plan:
        members = await self._directory(message, guild)
        roles = [role.name for role in guild.roles if not role.is_default() and not role.managed]
        replied = await self._replied_author(message, guild)
        system = PARSE_SYSTEM.format(
            requester=f"{message.author.id} — {_names(message.author)}",
            replied=(
                f"\nОн ответил на сообщение от: {replied[0]} — {replied[1]} — «ему», «ей», «его» значит этот человек."
                if replied
                else ""
            ),
            members="\n".join(f"{user_id} — {name}" for user_id, name in members.items()) or "(никого)",
            roles=", ".join(roles) or "(нет)",
        )
        completion = await self._llm.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": text}],
            max_tokens=400,
            temperature=0.0,
            models=runtime.models,
            providers=runtime.providers,
            purpose="moderation",
        )
        return parse_plan(completion.text)

    async def _replied_author(self, message: discord.Message, guild: discord.Guild) -> tuple[int, str] | None:
        """Who wrote the message this request answers, unless it is the bot itself."""
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None
        stored = await self._storage.get_message(message.channel.id, reference.message_id)
        if stored is None or stored.is_bot:
            return None
        cached = guild.get_member(stored.user_id)
        return stored.user_id, _names(cached) if cached is not None else stored.author

    async def _directory(self, message: discord.Message, guild: discord.Guild) -> dict[int, str]:
        """Who the model may mean: people mentioned, then those who wrote in the channel lately."""
        members: dict[int, str] = {}
        for user in message.mentions:
            members[user.id] = _names(user)
        since = int(time.time()) - _DIRECTORY_DAYS * 86400
        for stored in reversed(await self._storage.messages_since(message.channel.id, since, 20_000)):
            if not stored.is_bot and stored.user_id not in members and len(members) < _MAX_MEMBERS:
                cached = guild.get_member(stored.user_id)
                members[stored.user_id] = _names(cached) if cached is not None else stored.author
        return members

    async def _run(self, guild: discord.Guild, action: dict[str, Any], reason: str, protected: set[int]) -> str:
        kind = action["do"]
        try:
            if kind in ("mute", "unmute", "kick", "ban", "unban"):
                return await _discipline(guild, action, reason, protected)
            if kind == "nick":
                member = await _member(guild, action.get("user"))
                nick = action.get("nick")
                nick = nick.strip()[:32] if isinstance(nick, str) and nick.strip() else None
                await member.edit(nick=nick, reason=reason)
                return f"✅ {member.name}: ник «{nick}»" if nick else f"✅ {member.name}: ник сброшен"
            if kind in ("give_role", "create_role", "color"):
                name = _role_name(action)
                colour = parse_color(action.get("color"))
                if kind == "color" and colour is None:
                    raise _Refused(f"Не понял цвет для роли «{name}»")
                # The member first: no new role for someone who is not there.
                member = await _member(guild, action.get("user")) if kind == "give_role" else None
                role, created = await _ensure_role(guild, name, colour, reason)
                note = f"роль «{role.name}»" + (" создана" if created else "") + (f", цвет {colour}" if colour else "")
                if member is None:
                    return f"✅ {note[0].upper()}{note[1:]}"
                await member.add_roles(role, reason=reason)
                return f"✅ {member.name}: {note}"
            if kind == "take_role":
                member = await _member(guild, action.get("user"))
                role = _find_role(guild, _role_name(action))
                if role is None:
                    return f"⚠️ Роли «{_role_name(action)}» нет"
                await member.remove_roles(role, reason=reason)
                return f"✅ {member.name}: роль «{role.name}» снята"
            return f"⚠️ Не умею: {kind}"
        except _Refused as exc:
            return f"⚠️ {exc}"
        except discord.Forbidden:
            return (
                f"⚠️ {_describe(action)}: Discord не дал прав — моя роль должна быть выше роли участника "
                "и выше редактируемой роли, а ник владельца сервера бот менять не может"
            )
        except discord.HTTPException as exc:
            return f"⚠️ {_describe(action)}: Discord ответил ошибкой ({exc.status})"


class _Refused(Exception):
    pass


def _names(user: discord.abc.User | discord.Member) -> str:
    display = getattr(user, "display_name", user.name)
    return display if display == user.name else f"{display} ({user.name})"


def _role_name(action: dict[str, Any]) -> str:
    name = action.get("role")
    if not isinstance(name, str) or not name.strip():
        raise _Refused("Не понял название роли")
    return name.strip()[:100]


def _describe(action: dict[str, Any]) -> str:
    return {
        "nick": "Ник",
        "take_role": "Снять роль",
        "mute": "Мут",
        "unmute": "Размут",
        "kick": "Кик",
        "ban": "Бан",
        "unban": "Разбан",
    }.get(action["do"], "Роль")


_VERBS = {"ban": "забанить", "kick": "выгнать", "mute": "замутить", "unmute": "размутить", "unban": "разбанить"}


def _describe_plan(action: dict[str, Any], names: dict[int, str]) -> str:
    user = _user_id(action.get("user"))
    who = f"{names.get(user, 'кого-то')} (id {user})" if user is not None else "кого-то"
    verb = _VERBS.get(action["do"])
    if verb is None:
        return f"{_describe(action).lower()} — {who}"
    if action["do"] == "mute":
        return f"{verb} {who} на {_minutes(action)} мин"
    return f"{verb} {who}"


def _user_id(value: object) -> int | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _minutes(action: dict[str, Any]) -> int:
    minutes = action.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, int | float) or minutes <= 0:
        return 10
    return min(int(minutes), MAX_MUTE_MINUTES)


async def _discipline(guild: discord.Guild, action: dict[str, Any], reason: str, protected: set[int]) -> str:
    kind = action["do"]
    user = _user_id(action.get("user"))
    if user is None:
        raise _Refused("Не понял, о ком речь")
    if kind in ("mute", "kick", "ban") and user in protected:
        raise _Refused("Модераторов, владельца сервера, себя и того, кто просит, не трогаю")
    if kind == "unban":
        await guild.unban(discord.Object(id=user), reason=reason)
        return f"✅ id {user}: разбанен"
    if kind == "ban":
        member = guild.get_member(user)
        await guild.ban(member or discord.Object(id=user), reason=reason, delete_message_seconds=0)
        return f"✅ {member.name if member else f'id {user}'}: забанен"
    member = await _member(guild, user)
    if kind == "kick":
        await member.kick(reason=reason)
        return f"✅ {member.name}: выгнан с сервера"
    if kind == "mute":
        minutes = _minutes(action)
        await member.timeout(timedelta(minutes=minutes), reason=reason)
        return f"✅ {member.name}: мут на {minutes} мин"
    await member.timeout(None, reason=reason)
    return f"✅ {member.name}: мут снят"


async def _member(guild: discord.Guild, user_id: object) -> discord.Member:
    if isinstance(user_id, str) and user_id.isdigit():
        user_id = int(user_id)
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise _Refused("Не понял, о ком речь")
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        raise _Refused("Такого участника на сервере нет") from None


def _find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    wanted = name.casefold()
    return next((role for role in guild.roles if role.name.casefold() == wanted), None)


async def _ensure_role(
    guild: discord.Guild, name: str, colour: discord.Colour | None, reason: str
) -> tuple[discord.Role, bool]:
    """(the role, whether it was created). An existing role gets the colour if one was asked for."""
    role = _find_role(guild, name)
    if role is None:
        created = await guild.create_role(name=name, colour=colour or discord.Colour.default(), reason=reason)
        return created, True
    if role.managed or role.is_default():
        raise _Refused(f"Роль «{role.name}» служебная, её не трогаю")
    if colour is not None and role.colour != colour:
        role = await role.edit(colour=colour, reason=reason) or role
    return role, False
