import json
import time

from backseat.bot_config import DEFAULTS_KEY, BotConfig
from backseat.config import CoreSettings
from backseat.heartbeat import CHAT_TITLE_PREFIX, HEARTBEAT_KEY, beat, remember_chat_title
from backseat.storage import Storage
from tests.conftest import CHAT


async def test_a_beat_marks_the_bot_alive_and_publishes_its_defaults(settings: CoreSettings, storage: Storage) -> None:
    before = int(time.time())
    await beat(storage, BotConfig(storage, settings, platform="Discord"))
    assert before <= int(await storage.get_meta(HEARTBEAT_KEY) or 0) <= int(time.time())
    defaults = json.loads(await storage.get_meta(DEFAULTS_KEY) or "{}")
    assert defaults["platform"] == "Discord"
    assert defaults["models"] == settings.models


async def test_chat_titles_are_remembered_for_the_panel(storage: Storage) -> None:
    await remember_chat_title(storage, CHAT, "#общий")
    assert await storage.get_meta(f"{CHAT_TITLE_PREFIX}{CHAT}") == "#общий"
