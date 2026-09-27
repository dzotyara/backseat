from pathlib import Path

import pytest

from backseat.discord.settings import DiscordSettings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real environment variables outrank .env files; keep the developer's shell out of these tests."""
    for name in [*DiscordSettings.model_fields, "model_name"]:
        monkeypatch.delenv(name.upper(), raising=False)


def test_discord_overrides_the_shared_env(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text(
        "OPENROUTER_API_KEY=sk-shared\nDB_PATH=data/backseat.db\nALLOWED_CHAT_IDS=-1001\nBOT_NAMES=бэксит\n",
        encoding="utf-8",
    )
    overrides = tmp_path / ".env.discord"
    overrides.write_text(
        "DISCORD_BOT_TOKEN=token\nALLOWED_CHAT_IDS=100000000000000002\nDB_PATH=data/discord.db\nBOT_NAMES=\n",
        encoding="utf-8",
    )
    settings = DiscordSettings(_env_file=(shared, overrides))
    assert settings.openrouter_api_key.get_secret_value() == "sk-shared"
    assert settings.discord_bot_token.get_secret_value() == "token"
    assert settings.allowed_chat_ids == [100000000000000002]
    assert settings.db_path == Path("data/discord.db")
    assert settings.bot_names == ["бэксит"]  # an empty override keeps the shared value
    assert (settings.backfill_days, settings.digest_cooldown_seconds) == (90, 600)
    assert DiscordSettings.model_config["env_file"] == (".env", ".env.discord")


def test_example_file_is_valid() -> None:
    settings = DiscordSettings(
        _env_file=ROOT / ".env.discord.example", openrouter_api_key="sk-test", discord_bot_token="token"
    )
    assert settings.allowed_chat_ids
    assert settings.bot_names == ["ботяра", "botyara"]
    assert settings.focus_users == {}  # nobody is singled out by default on Discord
    assert settings.db_path == Path("data/discord.db")
    assert (ROOT / settings.persona_file).read_text(encoding="utf-8").strip()
