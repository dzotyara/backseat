import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord

from backseat.bot_config import BotConfig
from backseat.discord.moderation import Moderation, parse_color, parse_plan
from backseat.prompts import SYSTEM_TEMPLATE
from backseat.storage import Storage, StoredMessage
from tests.discord.fakes import BOT_ID, CHANNEL, IVAN, OWNER, PETYA, FakeLLM, http_error, make_settings


class FakeRole:
    def __init__(self, name: str, colour: int = 0, *, managed: bool = False, default: bool = False) -> None:
        self.name, self.colour, self.managed, self._default = name, discord.Colour(colour), managed, default

    def is_default(self) -> bool:
        return self._default

    async def edit(self, *, colour: discord.Colour, reason: str) -> "FakeRole":
        self.colour = colour
        return self


class FakeMember:
    def __init__(self, user_id: int, name: str, *, refuse: bool = False) -> None:
        self.id, self.name, self.display_name = user_id, name, name
        self.nick: str | None = None
        self.roles: list[FakeRole] = []
        self._refuse = refuse
        self.muted_for: timedelta | None = None
        self.kicked = False

    async def timeout(self, until: timedelta | None, *, reason: str) -> None:
        self.muted_for = until

    async def kick(self, *, reason: str) -> None:
        self.kicked = True

    async def edit(self, *, nick: str | None, reason: str) -> None:
        if self._refuse:
            raise http_error()
        self.nick = nick

    async def add_roles(self, role: FakeRole, *, reason: str) -> None:
        self.roles.append(role)

    async def remove_roles(self, role: FakeRole, *, reason: str) -> None:
        self.roles.remove(role)


class FakeGuild:
    def __init__(self, members: list[FakeMember], roles: list[FakeRole]) -> None:
        self.members = {member.id: member for member in members}
        self.roles = [FakeRole("@everyone", default=True), *roles]
        self.owner_id = 999
        self.me = SimpleNamespace(id=BOT_ID)
        self.banned: list[int] = []
        self.unbanned: list[int] = []

    async def ban(self, user: Any, *, reason: str, delete_message_seconds: int) -> None:
        self.banned.append(user.id)

    async def unban(self, user: Any, *, reason: str) -> None:
        self.unbanned.append(user.id)

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int) -> FakeMember:
        raise http_error(discord.NotFound, 404)

    async def create_role(self, *, name: str, colour: discord.Colour, reason: str) -> FakeRole:
        role = FakeRole(name, colour.value)
        self.roles.append(role)
        return role


def request(text: str, author: int = OWNER, guild: FakeGuild | None = None, reply_to: int | None = None) -> Any:
    return SimpleNamespace(
        id=1,
        content=text,
        clean_content=text,
        author=SimpleNamespace(id=author, name="dzotyara", display_name="Дзотяра"),
        mentions=[],
        channel=SimpleNamespace(id=CHANNEL),
        guild=guild,
        reference=SimpleNamespace(message_id=reply_to) if reply_to else None,
    )


class RecordingLLM(FakeLLM):
    def __init__(self, *answers: str) -> None:
        super().__init__(*answers)
        self.prompts: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], **options: Any) -> Any:
        self.prompts.append(messages)
        return await super().complete(messages, **options)


def plan(*actions: dict[str, Any], question: str | None = None) -> str:
    return "```json\n" + json.dumps({"actions": list(actions), "question": question}, ensure_ascii=False) + "\n```"


async def runtime(tmp_path: Path, storage: Storage, moderators: list[int]) -> Any:
    config = BotConfig(storage, make_settings(tmp_path), platform="Discord")
    await config.set_runtime(moderator_ids=moderators)
    return await config.runtime()


async def test_only_moderators_and_only_such_requests(tmp_path: Path, storage: Storage) -> None:
    rt = await runtime(tmp_path, storage, [OWNER])
    mod = Moderation(storage, FakeLLM())  # type: ignore[arg-type]
    assert mod.wants(request("ботяра, поменяй Ивокси ник на Антон"), rt)
    assert mod.wants(request("ботяра, забань Ивокси"), rt)
    assert not mod.wants(request("ботяра, как дела?"), rt)
    assert not mod.wants(request("ботяра, поменяй Ивокси ник на Антон", author=PETYA), rt)


async def test_nick_new_coloured_role_and_the_report(tmp_path: Path, storage: Storage) -> None:
    await storage.add_message(StoredMessage(CHANNEL, 5, IVAN, "IVOXY", "ку", None, False, 2_000_000_000))
    ivoxy = FakeMember(IVAN, "IVOXY")
    guild = FakeGuild([ivoxy], [FakeRole("Админ")])
    llm = FakeLLM(
        plan(
            {"do": "nick", "user": IVAN, "nick": "Антон"},
            {"do": "give_role", "user": str(IVAN), "role": "Морпех", "color": "#78866B"},
        )
    )
    report = await Moderation(storage, llm).handle(  # type: ignore[arg-type]
        request("ботяра, поменяй Ивокси ник на Антон, роль Морпех, цвет хаки", guild=guild),
        await runtime(tmp_path, storage, [OWNER]),
    )
    assert ivoxy.nick == "Антон"
    [role] = ivoxy.roles
    assert (role.name, role.colour.value) == ("Морпех", 0x78866B)
    assert report == "✅ IVOXY: ник «Антон»\n✅ IVOXY: роль «Морпех» создана, цвет #78866b"


async def test_existing_role_is_recoloured_not_duplicated(tmp_path: Path, storage: Storage) -> None:
    guild = FakeGuild([], [FakeRole("морпех", 0x111111)])
    llm = FakeLLM(plan({"do": "color", "role": "Морпех", "color": "78866B"}))
    report = await Moderation(storage, llm).handle(  # type: ignore[arg-type]
        request("ботяра, перекрась роль Морпех в хаки", guild=guild), await runtime(tmp_path, storage, [OWNER])
    )
    assert [r.name for r in guild.roles] == ["@everyone", "морпех"]
    assert guild.roles[1].colour.value == 0x78866B
    assert report == "✅ Роль «морпех», цвет #78866b"


async def test_refusals_are_reported_per_action(tmp_path: Path, storage: Storage) -> None:
    owner = FakeMember(PETYA, "Petya", refuse=True)
    guild = FakeGuild([owner], [FakeRole("Бот", managed=True)])
    llm = FakeLLM(
        plan(
            {"do": "nick", "user": PETYA, "nick": "Пётр"},
            {"do": "give_role", "user": 42, "role": "Морпех"},
            {"do": "color", "role": "Бот", "color": "#000001"},
            {"do": "explode", "user": PETYA},
        )
    )
    report = await Moderation(storage, llm).handle(  # type: ignore[arg-type]
        request("ботяра, ник, роль и цвет", guild=guild), await runtime(tmp_path, storage, [OWNER])
    )
    lines = (report or "").splitlines()
    assert lines[0].startswith("⚠️ Ник: Discord не дал прав")
    assert lines[1] == "⚠️ Такого участника на сервере нет"
    assert lines[2] == "⚠️ Роль «Бот» служебная, её не трогаю"
    assert lines[3] == "⚠️ Не умею: explode"
    assert [r.name for r in guild.roles] == ["@everyone", "Бот"]  # no «Морпех» for a missing member


async def test_not_a_request_or_a_question(tmp_path: Path, storage: Storage) -> None:
    rt = await runtime(tmp_path, storage, [OWNER])
    guild = FakeGuild([], [])
    mod = Moderation(storage, FakeLLM(plan(), plan(question="Кого из двух Иванов?"), "не JSON"))  # type: ignore[arg-type]
    assert await mod.handle(request("ботяра, какого цвета небо?", guild=guild), rt) is None
    assert await mod.handle(request("ботяра, дай Ивану роль", guild=guild), rt) == "Кого из двух Иванов?"
    assert await mod.handle(request("ботяра, роль", guild=guild), rt) is None
    report = await mod.handle(request("ботяра, роль", guild=guild), rt)  # the model is down
    assert report is not None and "недоступна" in report


def test_parsers() -> None:
    assert parse_plan('{"actions": [{"do": "nick"}, "мусор", {"x": 1}]}').actions == [{"do": "nick"}]
    assert parse_plan("[]") == parse_plan("")
    assert parse_color("#78866b") == discord.Colour(0x78866B)
    assert parse_color("хаки") is None and parse_color(None) is None


async def test_ban_waits_for_yes_and_no_cancels(tmp_path: Path, storage: Storage) -> None:
    await storage.add_message(StoredMessage(CHANNEL, 5, IVAN, "Крутой ник", "мама", None, False, 2_000_000_000))
    guild = FakeGuild([FakeMember(IVAN, "Крутой ник")], [])
    now = [0.0]
    ban = plan({"do": "ban", "user": IVAN})
    mod = Moderation(storage, FakeLLM(ban, ban), clock=lambda: now[0])  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage, [OWNER])

    ask = await mod.handle(request("ботяра, забань Крутого", guild=guild), rt)
    assert ask == f"Точно? забанить Крутой ник (id {IVAN}). Ответь «да» в течение 2 минут — или «нет»."
    assert guild.banned == []
    yes = request("Да, я хочу забанить его", guild=guild)
    assert mod.wants(yes, rt) and mod.awaits_answer(yes)  # an answer, though it names no nick or role
    assert await mod.handle(yes, rt) == "✅ Крутой ник: забанен"
    assert guild.banned == [IVAN]
    assert not mod.awaits_answer(yes)

    await mod.handle(request("ботяра, забань Крутого", guild=guild), rt)
    assert await mod.handle(request("нет", guild=guild), rt) == "Ок, отменил."
    assert guild.banned == [IVAN]


async def test_a_late_yes_does_nothing(tmp_path: Path, storage: Storage) -> None:
    guild = FakeGuild([FakeMember(IVAN, "Крутой ник")], [])
    now = [0.0]
    mod = Moderation(storage, FakeLLM(plan({"do": "kick", "user": IVAN})), clock=lambda: now[0])  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage, [OWNER])
    await mod.handle(request("ботяра, выгони Крутого", guild=guild), rt)
    now[0] += 121
    late = request("да", guild=guild)
    assert not mod.wants(late, rt)
    assert guild.members[IVAN].kicked is False


async def test_mute_unmute_unban_at_once_and_moderators_are_safe(tmp_path: Path, storage: Storage) -> None:
    ivan, petya = FakeMember(IVAN, "Ivan"), FakeMember(PETYA, "Petya")
    guild = FakeGuild([ivan, petya], [])
    llm = FakeLLM(
        plan({"do": "mute", "user": IVAN, "minutes": 60}, {"do": "unban", "user": 42}),
        plan({"do": "unmute", "user": IVAN}, {"do": "mute", "user": PETYA}, {"do": "ban", "user": OWNER}),
    )
    mod = Moderation(storage, llm)  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage, [OWNER, PETYA])
    report = await mod.handle(request("ботяра, замуть Ивана на час и разбань 42", guild=guild), rt)
    assert report == "✅ Ivan: мут на 60 мин\n✅ id 42: разбанен"
    assert ivan.muted_for == timedelta(minutes=60) and guild.unbanned == [42]

    ask = await mod.handle(request("ботяра, размуть Ивана, замуть Петю, забань себя", guild=guild), rt)
    assert ask is not None and ask.startswith("Точно?")  # a ban in the plan: everything waits for «да»
    lines = (await mod.handle(request("да", guild=guild), rt) or "").splitlines()
    assert lines[0] == "✅ Ivan: мут снят"
    assert lines[1].startswith("⚠️ Модераторов, владельца сервера")
    assert lines[2].startswith("⚠️ Модераторов, владельца сервера")
    assert petya.muted_for is None and guild.banned == []


async def test_the_answer_to_a_question_is_read_with_the_request(tmp_path: Path, storage: Storage) -> None:
    await storage.add_message(StoredMessage(CHANNEL, 5, IVAN, "умер в таркове", "мама", None, False, 2_000_000_000))
    ivan = FakeMember(IVAN, "умер в таркове")
    guild = FakeGuild([ivan], [])
    llm = RecordingLLM(plan(question="Кому выдать роль?"), plan({"do": "give_role", "user": IVAN, "role": "Хранитель"}))
    mod = Moderation(storage, llm)  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage, [OWNER])
    assert await mod.handle(request("ботяра, сделай ему роль Хранитель", guild=guild), rt) == "Кому выдать роль?"
    answer = request("Умер в таркове", guild=guild)
    assert mod.wants(answer, rt)  # no nick or role in it, and not addressed — still an answer
    assert await mod.handle(answer, rt) == "✅ умер в таркове: роль «Хранитель» создана"
    assert llm.prompts[1][1]["content"] == "ботяра, сделай ему роль Хранитель\nУточнение на твой вопрос: Умер в таркове"


async def test_me_and_him_are_explained_to_the_model(tmp_path: Path, storage: Storage) -> None:
    await storage.add_message(StoredMessage(CHANNEL, 7, IVAN, "умер в таркове", "роль дай", None, False, 2_000_000_000))
    llm = RecordingLLM(plan())
    mod = Moderation(storage, llm)  # type: ignore[arg-type]
    await mod.handle(
        request("ботяра, дай ему роль", guild=FakeGuild([], []), reply_to=7), await runtime(tmp_path, storage, [OWNER])
    )
    system = llm.prompts[0][0]["content"]
    assert f"Просит: {OWNER} — Дзотяра (dzotyara) — «мне»" in system
    assert f"ответил на сообщение от: {IVAN} — умер в таркове" in system


def test_the_chat_model_never_claims_moderation() -> None:
    assert "Никогда не пиши, что сделал или сейчас сделаешь" in SYSTEM_TEMPLATE


async def test_done_and_cancelled_requests_go_to_the_journal(tmp_path: Path, storage: Storage) -> None:
    await storage.add_message(StoredMessage(CHANNEL, 5, IVAN, "Крутой ник", "мама", None, False, 2_000_000_000))
    guild = FakeGuild([FakeMember(IVAN, "Крутой ник")], [])
    ban, nick = plan({"do": "ban", "user": IVAN}), plan({"do": "nick", "user": IVAN, "nick": "Антон"})
    mod = Moderation(storage, FakeLLM(nick, ban))  # type: ignore[arg-type]
    rt = await runtime(tmp_path, storage, [OWNER])

    await mod.handle(request("ботяра, ник Крутому — Антон", guild=guild), rt)
    await mod.handle(request("ботяра, забань Крутого", guild=guild), rt)
    await mod.handle(request("нет", guild=guild), rt)
    cancelled, done = await storage.latest_moderation(10)
    assert (done.request, done.result) == ("ботяра, ник Крутому — Антон", "✅ Крутой ник: ник «Антон»")
    assert done.moderator_id == OWNER and done.chat_id == CHANNEL
    assert cancelled.request == "ботяра, забань Крутого"
    assert cancelled.result == f"❌ отменено: забанить Крутой ник (id {IVAN})"
