from pathlib import Path

import httpx
import pytest

from backseat.config import DEFAULT_MODELS
from tests.web.conftest import BOT_MODELS, CHAT, OPENROUTER_KEY, PERSONA, BotDb, Panel, card, make_panel, msg


def test_dashboard_without_bots(panel: Panel) -> None:
    page = panel.client.get("/")
    assert page.status_code == 200
    for slug in ("telegram", "discord"):
        assert "не настроен" in card(page.text, slug)
    assert "backseat.db" in card(page.text, "telegram")
    # The panel never creates a database: an empty one would look like a bot that exists.
    assert not panel.telegram.path.exists() and not panel.discord.path.exists()


def test_dashboard_with_one_bot(panel: Panel) -> None:
    panel.telegram.setup(heartbeat_age=None, messages=[msg(1, "привет"), msg(2, "как дела")])
    page = panel.client.get("/").text
    telegram = card(page, "telegram")
    assert "нет данных" in telegram  # no heartbeat yet
    assert "пишет в чат" in telegram
    assert "2 сообщения" in telegram and "в 1 чате" in telegram
    assert f"/bots/telegram/memory/{CHAT}" in telegram
    assert all(model in telegram for model in BOT_MODELS)
    assert "не настроен" in card(page, "discord")


def test_dashboard_with_two_bots(panel: Panel) -> None:
    panel.telegram.setup(heartbeat_age=30, messages=[msg(1, "привет")])
    panel.discord.setup(heartbeat_age=600, messages=[msg(1, "hello", chat_id=987654321012345678)])
    panel.discord.run(lambda storage, config: config.set_runtime(paused=True))
    page = panel.client.get("/").text
    telegram, discord = card(page, "telegram"), card(page, "discord")
    assert "онлайн" in telegram and "пишет в чат" in telegram
    assert "не отвечает · 10 мин назад" in discord and "на паузе" in discord
    assert "/bots/discord/memory/987654321012345678" in discord
    assert 'aria-checked="true"' in telegram and 'aria-checked="false"' in discord


@pytest.mark.parametrize(
    ("value", "chip"), [("1789999990.5", "онлайн"), ("inf", "нет данных"), ("вчера", "нет данных")]
)
def test_heartbeat_values(panel: Panel, value: str, chip: str) -> None:
    panel.telegram.setup(heartbeat_age=None, meta={"heartbeat": value})
    assert chip in card(panel.client.get("/").text, "telegram")


def test_bot_that_has_not_published_its_defaults(panel: Panel) -> None:
    panel.telegram.setup(publish=False)
    settings = panel.client.get("/bots/telegram/settings").text
    assert "ещё не сообщил свои настройки по умолчанию" in settings
    assert DEFAULT_MODELS[0] in settings  # the built-in defaults meanwhile
    assert "Стандартный характер появится здесь" in panel.client.get("/bots/telegram/persona").text
    panel.client.post("/bots/telegram/pause", data={"paused": "1"})
    assert panel.telegram.runtime().paused  # the switch works regardless


def test_swapped_database_paths_are_flagged(tmp_path: Path, panel: Panel) -> None:
    BotDb(panel.discord.path, "Telegram", tmp_path / "persona.md").setup()
    page = panel.client.get("/").text
    assert "В этой базе записан бот Telegram, а панель открыла её как Discord." in card(page, "discord")


def test_unreadable_database(panel: Panel) -> None:
    panel.telegram.path.write_bytes(b"not a database, just bytes " * 100)
    panel.discord.setup()
    page = panel.client.get("/")
    assert page.status_code == 200
    assert "База бота сейчас не читается" in card(page.text, "telegram")
    assert "онлайн" in card(page.text, "discord")
    assert panel.client.get("/bots/telegram/persona").status_code == 503


def test_memory_pages(panel: Panel) -> None:
    messages = [msg(i, f"реплика-{i:03d}") for i in range(1, 61)]
    messages += [
        msg(61, "Кроссовки в отставке", is_bot=True, reply_to=60),
        msg(62, "<script>alert(1)</script>", user_id=111, author="Петя", reply_to=3),
    ]
    panel.telegram.setup(
        messages=messages, summary="## Хроника\nИван обещал бегать.", meta={f"chat_title:{CHAT}": "Друзья"}
    )
    listing = panel.client.get("/bots/telegram/memory").text
    assert "Друзья" in listing and "62 сообщения" in listing and "Иван, Петя" in listing
    assert f'href="/bots/telegram/memory/{CHAT}"' in listing

    page = panel.client.get(f"/bots/telegram/memory/{CHAT}").text
    assert "Иван обещал бегать." in page and "обновлена 1 ч назад" in page
    assert "50 из 62" in page
    assert "реплика-013" in page and "реплика-012" not in page  # only the latest 50
    assert "↩ Иван: реплика-060" in page  # a reply to a message on the page
    assert "ответ на более раннее сообщение" in page  # a reply to one that isn't
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page


def test_memory_of_an_empty_bot_and_unknown_chats(panel: Panel) -> None:
    panel.telegram.setup()
    assert "Бот пока ничего не запомнил" in panel.client.get("/bots/telegram/memory").text
    assert panel.client.get("/bots/telegram/memory/555").status_code == 404
    assert panel.client.get("/bots/telegram/memory/abc").status_code == 404


def test_spending_block(panel: Panel) -> None:
    page = panel.client.get("/").text
    request = panel.openrouter.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/key"
    assert request.headers["Authorization"] == f"Bearer {OPENROUTER_KEY}"
    assert "3 из 50" in page and "$0.0123" in page and "$1.50" in page

    panel.client.get("/")
    assert len(panel.openrouter.requests) == 1  # cached for a minute
    panel.clock.now += 61
    panel.client.get("/")
    assert len(panel.openrouter.requests) == 2


@pytest.mark.parametrize(
    ("status", "payload", "problem"),
    [
        (401, {"error": {"message": "No auth credentials found"}}, "OpenRouter не принял ключ"),
        (500, {"error": "boom"}, "OpenRouter ответил ошибкой 500"),
        (200, {"data": "weird"}, "непонятный ответ"),
        (200, ["not", "an", "object"], "OpenRouter не отвечает"),
    ],
)
def test_spending_problems(panel: Panel, status: int, payload: object, problem: str) -> None:
    panel.openrouter.status, panel.openrouter.payload = status, payload
    page = panel.client.get("/")
    assert page.status_code == 200
    assert problem in page.text


def test_spending_when_openrouter_is_unreachable(panel: Panel) -> None:
    panel.openrouter.error = httpx.ConnectTimeout("slow")
    assert "OpenRouter не отвечает" in panel.client.get("/").text


def test_spending_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    panel = make_panel(tmp_path, openrouter_api_key=None)
    assert "OPENROUTER_API_KEY не задан" in panel.client.get("/").text
    assert panel.openrouter.requests == []


def test_default_persona_is_shown(panel: Panel) -> None:
    panel.telegram.setup()
    page = panel.client.get("/bots/telegram/persona").text
    assert PERSONA.splitlines()[1] in page
