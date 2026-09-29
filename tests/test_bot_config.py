import json
from collections.abc import AsyncIterator
from dataclasses import fields
from pathlib import Path

import pytest

from backseat.bot_config import (
    DEFAULTS_KEY,
    GLOBAL,
    RUNTIME_KEY,
    SETTINGS_FIELDS,
    BotConfig,
    Runtime,
    check_runtime_value,
)
from backseat.config import CoreSettings
from backseat.prompts import SYSTEM_TEMPLATE
from backseat.storage import Storage

MODELS = ["paid/model", "free/model:free"]


def make_settings(tmp_path: Path, **overrides: object) -> CoreSettings:
    persona = tmp_path / "persona.md"
    if not persona.exists():
        persona.write_text("Ты — тестовый бот.\n", encoding="utf-8")
    values: dict[str, object] = {
        "openrouter_api_key": "sk-test",
        "models": MODELS,
        "bot_names": ["бэксит", "ботяра"],
        "persona_file": persona,
        "unprompted_cooldown_seconds": 60.0,
        "reactions_enabled": True,
        "weekly_digest": True,
    }
    values.update(overrides)
    return CoreSettings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    store = Storage(tmp_path / "bot.db")
    await store.connect()
    yield store
    await store.close()


@pytest.fixture
def config(tmp_path: Path, storage: Storage) -> BotConfig:
    return BotConfig(storage, make_settings(tmp_path))


async def stored_overrides(storage: Storage) -> object:
    raw = await storage.get_setting(GLOBAL, RUNTIME_KEY)
    return None if raw is None else json.loads(raw)


async def test_runtime_defaults_come_from_settings(config: BotConfig) -> None:
    settings = CoreSettings.model_construct()  # the built-in defaults
    assert await config.runtime() == Runtime(
        paused=False,
        allowed_chat_ids=[],
        moderator_ids=[],
        models=MODELS,
        providers=[],
        max_tokens=settings.max_tokens,
        unprompted_cooldown_seconds=60.0,
        reply_freeze_seconds=settings.reply_freeze_seconds,
        precheck_context_tokens=0,
        reactions_enabled=True,
        weekly_digest=True,
        recent_context_tokens=settings.recent_context_tokens,
        author_history_tokens=settings.author_history_tokens,
        focus_history_tokens=settings.focus_history_tokens,
        summary_enabled=True,
        system_template=SYSTEM_TEMPLATE,  # empty SYSTEM_TEMPLATE setting = the built-in rules
        images_enabled=True,
        pollinations_models=["zimage", "flux"],
        images_per_user_per_day=0,
        paid_images_per_day=0,
        image_models=["openai/gpt-5-image-mini"],
    )


async def test_only_overrides_are_stored_and_none_resets(config: BotConfig, storage: Storage) -> None:
    await config.set_runtime(paused=True, models=[" a/b ", "c/d", "a/b", ""], unprompted_cooldown_seconds=5)
    runtime = await config.runtime()
    assert (runtime.paused, runtime.models, runtime.unprompted_cooldown_seconds) == (True, ["a/b", "c/d"], 5.0)
    assert runtime.reactions_enabled and runtime.weekly_digest  # untouched: still the settings
    assert await stored_overrides(storage) == {
        "paused": True,
        "models": ["a/b", "c/d"],
        "unprompted_cooldown_seconds": 5.0,
    }

    # None resets a field; a value equal to the default is no override either.
    await config.set_runtime(models=None, unprompted_cooldown_seconds=60, reactions_enabled=True)
    assert await stored_overrides(storage) == {"paused": True}
    await config.set_runtime(paused=False)
    assert await stored_overrides(storage) is None
    assert await config.runtime() == config.default_runtime()


async def test_fields_without_override_follow_the_settings(tmp_path: Path, storage: Storage) -> None:
    await BotConfig(storage, make_settings(tmp_path)).set_runtime(weekly_digest=False)
    # The bot restarts with another .env: the override stays, everything else follows the new settings.
    runtime = await BotConfig(storage, make_settings(tmp_path, unprompted_cooldown_seconds=90.0)).runtime()
    assert runtime.unprompted_cooldown_seconds == 90.0
    assert runtime.weekly_digest is False


@pytest.mark.parametrize(
    "change",
    [
        {"models": []},
        {"models": ["  "]},
        {"models": "a/b"},
        {"models": ["a/b", 1]},
        {"unprompted_cooldown_seconds": -1},
        {"unprompted_cooldown_seconds": float("nan")},
        {"unprompted_cooldown_seconds": float("inf")},
        {"unprompted_cooldown_seconds": True},
        {"unprompted_cooldown_seconds": "60"},
        {"paused": "yes"},
        {"reactions_enabled": 1},
    ],
)
async def test_invalid_values_are_refused_and_nothing_is_stored(
    config: BotConfig, storage: Storage, change: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        await config.set_runtime(weekly_digest=False, **change)
    assert await stored_overrides(storage) is None


async def test_unknown_setting_is_a_type_error(config: BotConfig) -> None:
    with pytest.raises(TypeError):
        await config.set_runtime(volume=11)
    with pytest.raises(TypeError):
        await config.set_runtime(volume=None)


def test_every_runtime_field_is_checked() -> None:
    for field in fields(Runtime):
        with pytest.raises(ValueError):
            check_runtime_value(field.name, object())


async def test_broken_stored_values_fall_back_to_the_defaults(config: BotConfig, storage: Storage) -> None:
    for raw in ("{not json", '["paused"]', "null"):
        await storage.set_setting(GLOBAL, RUNTIME_KEY, raw)
        assert await config.runtime() == config.default_runtime()
    broken = {"paused": True, "models": [], "volume": 11, "unprompted_cooldown_seconds": -5}
    await storage.set_setting(GLOBAL, RUNTIME_KEY, json.dumps(broken))
    runtime = await config.runtime()
    assert runtime.paused is True  # the valid override survives
    assert runtime.models == MODELS
    assert runtime.unprompted_cooldown_seconds == 60.0


async def test_publish_defaults_writes_the_defaults_not_the_overrides(tmp_path: Path, storage: Storage) -> None:
    config = BotConfig(storage, make_settings(tmp_path, bot_names=["бот"], weekly_digest=False), platform="Discord")
    await config.set_persona("Свой характер")
    await config.set_names(["железяка"])
    await config.set_runtime(paused=True, models=["x/y"])
    await config.publish_defaults()
    published = json.loads(await storage.get_meta(DEFAULTS_KEY) or "")
    assert published["platform"] == "Discord" and published["names"] == ["бот"]
    assert published["persona"] == "Ты — тестовый бот."
    assert (published["models"], published["weekly_digest"]) == (MODELS, False)  # the defaults, not x/y
    assert set(SETTINGS_FIELDS) <= set(published)


async def test_publish_defaults_picks_up_an_edited_persona_file(tmp_path: Path, storage: Storage) -> None:
    settings = make_settings(tmp_path)
    config = BotConfig(storage, settings)
    await config.publish_defaults()
    settings.persona_file.write_text("Новый характер", encoding="utf-8")
    await config.publish_defaults()
    published = json.loads(await storage.get_meta(DEFAULTS_KEY) or "")
    assert published["persona"] == "Новый характер"
    assert published["platform"] == "Telegram"


async def test_name_pattern_follows_names_changed_by_another_process(tmp_path: Path, storage: Storage) -> None:
    config = BotConfig(storage, make_settings(tmp_path))
    pattern = await config.name_pattern()
    assert pattern is not None and pattern.search("ботяра, ты тут?")
    assert await config.name_pattern() is pattern  # same names: compiled once

    panel = Storage(tmp_path / "bot.db")  # the web panel: another connection to the same file
    await panel.connect()
    try:
        await BotConfig(panel, make_settings(tmp_path)).set_names(["железяка"])
        pattern = await config.name_pattern()
        assert pattern is not None and pattern.search("железяка, привет") and not pattern.search("ботяра, ты тут?")
        await BotConfig(panel, make_settings(tmp_path)).set_names(None)
    finally:
        await panel.close()
    pattern = await config.name_pattern()
    assert pattern is not None and pattern.search("ботяра, ты тут?")
