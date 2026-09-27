"""The panel has no login: it must leak no secrets and do nothing on behalf of other web pages."""

from pathlib import Path

import pytest

from backseat.web.settings import WebSettings
from tests.web.conftest import CHAT, SECRETS, Panel, make_panel, msg


def test_no_secret_on_any_page(panel: Panel) -> None:
    panel.telegram.setup(messages=[msg(1, "привет"), msg(2, "ответ", is_bot=True, reply_to=1)], summary="Сводка.")
    panel.discord.setup(publish=False)
    client = panel.client
    pages = [
        client.get(path)
        for path in (
            "/",
            "/bots/telegram",
            "/bots/telegram/persona",
            "/bots/telegram/settings",
            "/bots/telegram/memory",
            f"/bots/telegram/memory/{CHAT}",
            "/bots/discord/persona",
            "/bots/discord/settings",
            "/bots/discord/memory",
            "/bots/nope/persona",
            "/bots/telegram/memory/404",
            "/static/style.css",
            "/nowhere",
        )
    ]
    pages += [
        client.post("/bots/telegram/persona", data={"persona": "Свой характер"}),
        client.get("/bots/telegram/persona/reset"),
        client.post("/bots/telegram/persona", data={"persona": ""}),
        client.post("/bots/telegram/settings", data={"names": "", "models": "", "unprompted_cooldown_seconds": "x"}),
        client.post(
            "/bots/telegram/settings", data={"names": "бот", "models": "a/b", "unprompted_cooldown_seconds": "1"}
        ),
        client.post("/bots/telegram/pause", data={"paused": "1"}),
    ]
    assert len(panel.openrouter.requests) == 1  # the spending block was rendered
    for page in pages:
        for secret in SECRETS:
            assert secret not in page.text, (page.url, secret)
            assert all(secret not in value for value in page.headers.values())


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8090", "localhost.evil.example", "10.0.0.5:8090"])
def test_foreign_host_names_are_refused(panel: Panel, host: str) -> None:
    # DNS rebinding: a web page on a name that resolves to 127.0.0.1 reaches the panel with its own Host.
    response = panel.client.get("/", headers={"Host": host})
    assert response.status_code == 400
    assert panel.openrouter.requests == []


@pytest.mark.parametrize("host", ["localhost:8090", "127.0.0.1:8090", "[::1]:8090", "LOCALHOST"])
def test_loopback_host_names_are_fine(panel: Panel, host: str) -> None:
    assert panel.client.get("/", headers={"Host": host}).status_code == 200


def test_web_host_is_allowed_too(tmp_path: Path) -> None:
    panel = make_panel(tmp_path, web_host="192.168.1.5")
    assert panel.client.get("/", headers={"Host": "192.168.1.5:8090"}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},  # e.g. another dev server on localhost:3000
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ],
)
def test_posts_from_other_sites_are_refused(panel: Panel, headers: dict[str, str]) -> None:
    panel.telegram.setup()
    response = panel.client.post("/bots/telegram/pause", data={"paused": "1"}, headers=headers)
    assert response.status_code == 403
    assert not panel.telegram.runtime().paused


def test_posts_from_the_panel_itself_pass(panel: Panel) -> None:
    panel.telegram.setup()
    headers = {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost"}
    assert panel.client.post("/bots/telegram/pause", data={"paused": "1"}, headers=headers).status_code == 200
    assert panel.telegram.runtime().paused


def test_pages_cannot_be_framed_or_run_foreign_scripts(panel: Panel) -> None:
    response = panel.client.get("/")
    assert response.headers["X-Frame-Options"] == "DENY"
    policy = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy and "default-src 'self'" in policy
    assert "<script" not in response.text


def test_settings_come_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_DB_PATH", str(tmp_path / "tg.db"))
    monkeypatch.setenv("DISCORD_DB_PATH", str(tmp_path / "dc.db"))
    monkeypatch.setenv("WEB_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB_PORT", "9000")
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS[0])
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRETS[1])
    settings = WebSettings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.telegram_db_path, settings.discord_db_path) == (tmp_path / "tg.db", tmp_path / "dc.db")
    assert (settings.web_host, settings.web_port) == ("0.0.0.0", 9000)
    assert SECRETS[0] not in repr(settings) and SECRETS[1] not in repr(settings)


def test_defaults_when_nothing_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TELEGRAM_DB_PATH", "DISCORD_DB_PATH", "WEB_HOST", "WEB_PORT", "OPENROUTER_API_KEY", "TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    settings = WebSettings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.web_host, settings.web_port) == ("127.0.0.1", 8090)
    assert (settings.telegram_db_path, settings.discord_db_path) == (Path("data/backseat.db"), Path("data/discord.db"))
    assert settings.openrouter_api_key is None
