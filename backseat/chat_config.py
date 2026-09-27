"""Per-chat persona and names, changeable with /prompt and /names; defaults come from settings."""

import json
import logging
import re

from backseat.config import Settings
from backseat.prompts import FALLBACK_PERSONA
from backseat.storage import Storage
from backseat.triggers import compile_names

log = logging.getLogger(__name__)

PERSONA_KEY = "persona"
NAMES_KEY = "names"


class ChatConfig:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings
        self._name_patterns: dict[int, re.Pattern[str] | None] = {}

    def default_persona(self) -> str:
        # Read on every use, so edits to prompts/persona.md apply without a restart.
        try:
            text = self._settings.persona_file.read_text(encoding="utf-8").strip()
        except OSError:
            log.warning("Persona file %s is unreadable, using the built-in one", self._settings.persona_file)
            return FALLBACK_PERSONA
        return text or FALLBACK_PERSONA

    async def persona(self, chat_id: int) -> str:
        return await self._storage.get_setting(chat_id, PERSONA_KEY) or self.default_persona()

    async def has_custom_persona(self, chat_id: int) -> bool:
        return await self._storage.get_setting(chat_id, PERSONA_KEY) is not None

    async def set_persona(self, chat_id: int, text: str | None) -> None:
        await self._storage.set_setting(chat_id, PERSONA_KEY, text)

    async def names(self, chat_id: int) -> list[str]:
        raw = await self._storage.get_setting(chat_id, NAMES_KEY)
        return json.loads(raw) if raw else list(self._settings.bot_names)

    async def set_names(self, chat_id: int, names: list[str] | None) -> None:
        value = json.dumps(names, ensure_ascii=False) if names is not None else None
        await self._storage.set_setting(chat_id, NAMES_KEY, value)
        self._name_patterns.pop(chat_id, None)

    async def name_pattern(self, chat_id: int) -> re.Pattern[str] | None:
        if chat_id not in self._name_patterns:
            self._name_patterns[chat_id] = compile_names(await self.names(chat_id))
        return self._name_patterns[chat_id]
