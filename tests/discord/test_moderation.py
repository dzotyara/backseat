import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord

from backseat.bot_config import BotConfig
from backseat.discord.moderation import Moderation, parse_color, parse_plan
from backseat.storage import Storage, StoredMessage
from tests.discord.fakes import CHANNEL, IVAN, OWNER, PETYA, FakeLLM, http_error, make_settings


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

    def get_member(self, user_id: int) -> FakeMember | None:
        return self.members.get(user_id)

    async def fetch_member(self, user_id: int) -> FakeMember:
        raise http_error(discord.NotFound, 404)

    async def create_role(self, *, name: str, colour: discord.Colour, reason: str) -> FakeRole:
        role = FakeRole(name, colour.value)
        self.roles.append(role)
        return role


def request(text: str, author: int = OWNER, guild: FakeGuild | None = None) -> Any:
    return SimpleNamespace(
        id=1,
        content=text,
        clean_content=text,
        author=SimpleNamespace(id=author, display_name="Дзотяра"),
        mentions=[],
        channel=SimpleNamespace(id=CHANNEL),
        guild=guild,
    )


def plan(*actions: dict[str, Any], question: str | None = None) -> str:
    return "```json\n" + json.dumps({"actions": list(actions), "question": question}, ensure_ascii=False) + "\n```"


async def runtime(tmp_path: Path, storage: Storage, moderators: list[int]) -> Any:
    config = BotConfig(storage, make_settings(tmp_path), platform="Discord")
    await config.set_runtime(moderator_ids=moderators)
    return await config.runtime()


async def test_only_moderators_and_only_such_requests(tmp_path: Path, storage: Storage) -> None:
    rt = await runtime(tmp_path, storage, [OWNER])
    assert Moderation.wants(request("ботяра, поменяй Ивокси ник на Антон"), rt)
    assert not Moderation.wants(request("ботяра, как дела?"), rt)
    assert not Moderation.wants(request("ботяра, поменяй Ивокси ник на Антон", author=PETYA), rt)


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
            {"do": "ban", "user": PETYA},
        )
    )
    report = await Moderation(storage, llm).handle(  # type: ignore[arg-type]
        request("ботяра, ник, роль и цвет", guild=guild), await runtime(tmp_path, storage, [OWNER])
    )
    lines = (report or "").splitlines()
    assert lines[0].startswith("⚠️ Ник: Discord не дал прав")
    assert lines[1] == "⚠️ Такого участника на сервере нет"
    assert lines[2] == "⚠️ Роль «Бот» служебная, её не трогаю"
    assert lines[3] == "⚠️ Не умею: ban"
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
