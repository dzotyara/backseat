"""Bot-wide persona and names, changeable with /prompt (owner, in private) and /names, and runtime
behaviour changeable in the web panel. Defaults come from settings and prompts/persona.md."""

import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Self

from backseat.config import CoreSettings
from backseat.prompts import FALLBACK_PERSONA, SYSTEM_TEMPLATE
from backseat.storage import Storage
from backseat.triggers import compile_names

log = logging.getLogger(__name__)

# Bot-wide values live in chat_settings under this pseudo chat id.
GLOBAL = 0
PERSONA_KEY = "persona"
NAMES_KEY = "names"
RUNTIME_KEY = "runtime"  # JSON with only the overridden Runtime fields
# meta key with what the bot uses when nothing is overridden, for the web panel in another process.
DEFAULTS_KEY = "defaults"
# The Runtime fields that default to the same-named .env settings: all but the pause. The bot publishes
# their defaults for the web panel, and the panel's settings page edits them.
SETTINGS_FIELDS = (
    "allowed_chat_ids",
    "moderator_ids",
    "models",
    "providers",
    "max_tokens",
    "unprompted_cooldown_seconds",
    "reply_freeze_seconds",
    "precheck_context_tokens",
    "reactions_enabled",
    "weekly_digest",
    "recent_context_tokens",
    "author_history_tokens",
    "focus_history_tokens",
    "summary_enabled",
    "system_template",
    "images_enabled",
    "pollinations_models",
    "images_per_user_per_day",
    "paid_images_per_day",
    "image_models",
)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Behaviour the owner can change while the bot runs. A paused bot keeps reading and
    remembering the chat but writes nothing: no replies, no reactions, no digest."""

    paused: bool
    allowed_chat_ids: list[int]  # empty = every chat
    moderator_ids: list[int]  # may rename members and manage roles by asking the bot (Discord)
    models: list[str]
    providers: list[str]
    max_tokens: int  # per answer or comment
    unprompted_cooldown_seconds: float
    reply_freeze_seconds: float
    precheck_context_tokens: int
    reactions_enabled: bool
    weekly_digest: bool
    recent_context_tokens: int
    author_history_tokens: int
    focus_history_tokens: int
    summary_enabled: bool
    system_template: str
    images_enabled: bool
    pollinations_models: list[str]  # with a Pollinations key: tried in order
    images_per_user_per_day: int  # 0 = no limit
    paid_images_per_day: int  # 0 = never pay for a picture
    image_models: list[str]

    @classmethod
    def from_settings(cls, settings: CoreSettings) -> Self:
        values = {name: getattr(settings, name) for name in SETTINGS_FIELDS}
        values["system_template"] = values["system_template"] or SYSTEM_TEMPLATE
        return cls(paused=False, **{name: list(v) if isinstance(v, list) else v for name, v in values.items()})


def parse_names(text: str) -> list[str]:
    """Names as the owner types them: separated by commas, semicolons or line breaks, no repeats."""
    return list(dict.fromkeys(name.strip() for name in re.split(r"[,;\n]+", text) if name.strip()))


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected true or false")
    return value


def _words(value: object) -> list[str]:
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError("expected a list of strings")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _models(value: object) -> list[str]:
    models = _words(value)
    if not models:
        raise ValueError("at least one model is required")
    return models


def _ids(value: object) -> list[int]:
    if not isinstance(value, list | tuple) or not all(isinstance(i, int) and not isinstance(i, bool) for i in value):
        raise ValueError("expected a list of ids")
    return list(dict.fromkeys(value))


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError("expected a number of seconds >= 0")
    return float(value)


def _integer(low: int, high: int) -> Callable[[object], int]:
    def check(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"expected a whole number from {low} to {high}")
        return value

    return check


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-empty text")
    return value.strip()


MAX_TOKENS_RANGE = (16, 16_000)
BUDGET_RANGE = (0, 200_000)
IMAGES_RANGE = (0, 10_000)

_CHECKS: dict[str, Callable[[object], Any]] = {
    "paused": _flag,
    "allowed_chat_ids": _ids,
    "moderator_ids": _ids,
    "models": _models,
    "providers": _words,
    "max_tokens": _integer(*MAX_TOKENS_RANGE),
    "unprompted_cooldown_seconds": _seconds,
    "reply_freeze_seconds": _seconds,
    "precheck_context_tokens": _integer(*BUDGET_RANGE),
    "reactions_enabled": _flag,
    "weekly_digest": _flag,
    "recent_context_tokens": _integer(*BUDGET_RANGE),
    "author_history_tokens": _integer(*BUDGET_RANGE),
    "focus_history_tokens": _integer(*BUDGET_RANGE),
    "summary_enabled": _flag,
    "system_template": _text,
    "images_enabled": _flag,
    "pollinations_models": _words,
    "images_per_user_per_day": _integer(*IMAGES_RANGE),
    "paid_images_per_day": _integer(*IMAGES_RANGE),
    "image_models": _models,
}


def check_runtime_value(name: str, value: object) -> Any:
    """`value` normalized for the Runtime field `name`. ValueError if it doesn't fit,
    TypeError if there is no such field."""
    check = _CHECKS.get(name)
    if check is None:
        raise TypeError(f"unknown runtime setting: {name}")
    return check(value)


class BotConfig:
    def __init__(self, storage: Storage, settings: CoreSettings, *, platform: str = "Telegram") -> None:
        self._storage = storage
        self._settings = settings
        self.platform = platform
        self._name_pattern: re.Pattern[str] | None = None
        self._name_pattern_names: tuple[str, ...] | None = None

    # --- defaults ---

    def default_persona(self) -> str:
        # Read on every use, so edits to prompts/persona.md apply without a restart.
        try:
            text = self._settings.persona_file.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("Persona file %s is unreadable, using the built-in one", self._settings.persona_file)
            return FALLBACK_PERSONA
        return text or FALLBACK_PERSONA

    def default_names(self) -> list[str]:
        return list(self._settings.bot_names)

    def default_runtime(self) -> Runtime:
        return Runtime.from_settings(self._settings)

    async def publish_defaults(self) -> None:
        """Store the defaults in meta: the web panel runs in another process and knows neither
        this bot's .env nor its persona file. Call at startup."""
        runtime = self.default_runtime()
        value = json.dumps(
            {
                "platform": self.platform,
                "names": self.default_names(),
                "persona": self.default_persona(),
                **{name: getattr(runtime, name) for name in SETTINGS_FIELDS},
            },
            ensure_ascii=False,
        )
        if await self._storage.get_meta(DEFAULTS_KEY) != value:
            await self._storage.set_meta(DEFAULTS_KEY, value)

    # --- persona and names ---

    async def persona(self) -> str:
        return await self._storage.get_setting(GLOBAL, PERSONA_KEY) or self.default_persona()

    async def has_custom_persona(self) -> bool:
        return await self._storage.get_setting(GLOBAL, PERSONA_KEY) is not None

    async def set_persona(self, text: str | None) -> None:
        await self._storage.set_setting(GLOBAL, PERSONA_KEY, text)

    async def names(self) -> list[str]:
        raw = await self._storage.get_setting(GLOBAL, NAMES_KEY)
        return json.loads(raw) if raw else self.default_names()

    async def set_names(self, names: list[str] | None) -> None:
        value = json.dumps(names, ensure_ascii=False) if names is not None else None
        await self._storage.set_setting(GLOBAL, NAMES_KEY, value)

    async def name_pattern(self) -> re.Pattern[str] | None:
        # Keyed by the names themselves: the web panel changes them from another process.
        names = tuple(await self.names())
        if names != self._name_pattern_names:
            self._name_pattern = compile_names(names)
            self._name_pattern_names = names
        return self._name_pattern

    # --- runtime behaviour ---

    async def runtime(self) -> Runtime:
        return replace(self.default_runtime(), **await self._runtime_overrides())

    async def set_runtime(self, **changes: object) -> None:
        """Override Runtime fields; None resets a field to its default. A value equal to the
        default is not stored, so that field keeps following the settings."""
        defaults = self.default_runtime()
        overrides = await self._runtime_overrides()
        for name, value in changes.items():
            if name not in _CHECKS:
                raise TypeError(f"unknown runtime setting: {name}")
            checked = None if value is None else _CHECKS[name](value)
            if checked is None or checked == getattr(defaults, name):
                overrides.pop(name, None)
            else:
                overrides[name] = checked
        value = json.dumps(overrides, ensure_ascii=False) if overrides else None
        await self._storage.set_setting(GLOBAL, RUNTIME_KEY, value)

    async def _runtime_overrides(self) -> dict[str, Any]:
        raw = await self._storage.get_setting(GLOBAL, RUNTIME_KEY)
        try:
            stored = json.loads(raw) if raw else {}
        except ValueError:
            stored = None
        if not isinstance(stored, dict):
            log.warning("Ignoring unreadable runtime settings: %.200s", raw)
            return {}
        overrides = {}
        for name, value in stored.items():
            try:
                overrides[name] = check_runtime_value(name, value)
            except (TypeError, ValueError):
                log.warning("Ignoring runtime setting %s=%r", name, value)
        return overrides
