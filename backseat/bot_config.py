"""Bot-wide persona and names, changeable with /prompt (owner, in private) and /names.
Defaults come from settings and prompts/persona.md."""

import json
import logging
import re

from backseat.config import Settings
from backseat.prompts import FALLBACK_PERSONA
from backseat.storage import Storage
from backseat.triggers import compile_names

log = logging.getLogger(__name__)

# Bot-wide values live in chat_settings under this pseudo chat id.
GLOBAL = 0
PERSONA_KEY = "persona"
NAMES_KEY = "names"


class BotConfig:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._name_pattern: re.Pattern[str] | None = None
        self._name_pattern_stale = True

    def default_persona(self) -> str:
        # Read on every use, so edits to prompts/persona.md apply without a restart.
        try:
            text = self._settings.persona_file.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("Persona file %s is unreadable, using the built-in one", self._settings.persona_file)
            return FALLBACK_PERSONA
        return text or FALLBACK_PERSONA

    async def persona(self) -> str:
        return await self._storage.get_setting(GLOBAL, PERSONA_KEY) or self.default_persona()

    async def has_custom_persona(self) -> bool:
        return await self._storage.get_setting(GLOBAL, PERSONA_KEY) is not None

    async def set_persona(self, text: str | None) -> None:
        await self._storage.set_setting(GLOBAL, PERSONA_KEY, text)

    async def names(self) -> list[str]:
        raw = await self._storage.get_setting(GLOBAL, NAMES_KEY)
        return json.loads(raw) if raw else list(self._settings.bot_names)

    async def set_names(self, names: list[str] | None) -> None:
        value = json.dumps(names, ensure_ascii=False) if names is not None else None
        await self._storage.set_setting(GLOBAL, NAMES_KEY, value)
        self._name_pattern_stale = True

    async def name_pattern(self) -> re.Pattern[str] | None:
        if self._name_pattern_stale:
            self._name_pattern = compile_names(await self.names())
            self._name_pattern_stale = False
        return self._name_pattern
