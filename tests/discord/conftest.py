from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from backseat.discord.settings import DiscordSettings
from backseat.storage import Storage
from tests.discord.fakes import make_settings


@pytest.fixture
def settings(tmp_path: Path) -> DiscordSettings:
    return make_settings(tmp_path)


@pytest.fixture
async def storage(settings: DiscordSettings) -> AsyncIterator[Storage]:
    store = Storage(settings.db_path)
    await store.connect()
    yield store
    await store.close()
