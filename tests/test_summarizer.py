import dataclasses
import re
from pathlib import Path

import pytest

from backseat.bot_config import BotConfig
from backseat.config import CoreSettings
from backseat.llm import Completion, LLMError
from backseat.render import LineFormatter
from backseat.storage import Storage
from backseat.summarizer import Summarizer
from tests.conftest import CHAT, FakeLLM, make_settings, msg


def make_summarizer(
    storage: Storage, llm: FakeLLM, settings: CoreSettings, formatter: LineFormatter, platform: str = "Telegram"
) -> Summarizer:
    bot_config = BotConfig(storage, settings, platform=platform)
    return Summarizer(storage, llm, settings, formatter, bot_config=bot_config)  # type: ignore[arg-type]


async def fill(storage: Storage, count: int) -> None:
    for i in range(1, count + 1):
        await storage.add_message(msg(i, f"сообщение номер {i} про планы на выходные"))


def chunk_lines(prompt: str) -> list[str]:
    return [line for line in prompt.splitlines() if re.match(r"#\d+ ", line)]


async def test_short_history_is_not_summarized(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path)
    await fill(storage, 20)
    llm = FakeLLM()
    assert not await make_summarizer(storage, llm, settings, formatter).update_once(CHAT)
    assert llm.calls == []


async def test_oldest_chunk_is_folded_into_the_summary(
    tmp_path: Path, storage: Storage, formatter: LineFormatter
) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    await fill(storage, 40)
    llm = FakeLLM("Сводка 1", "Сводка 2")
    summarizer = make_summarizer(storage, llm, settings, formatter)

    assert await summarizer.update_once(CHAT)
    first = await storage.get_summary(CHAT)
    assert first is not None and first.text == "Сводка 1"
    prompt = llm.prompt_text()
    assert "долговременную память группового чата в Telegram" in prompt
    assert "ТЕКУЩАЯ СВОДКА:\n(пока пусто)" in prompt
    assert "#1 " in prompt and "#40 " not in prompt
    assert "Про Иван — подробнее всех" in prompt
    assert "не больше 900 слов, все четыре раздела." in prompt
    assert 1 < first.upto_message_id < 40

    assert await summarizer.update_once(CHAT)
    second = await storage.get_summary(CHAT)
    assert second is not None and second.upto_message_id > first.upto_message_id
    assert "ТЕКУЩАЯ СВОДКА (слов: 2):\nСводка 1" in llm.prompt_text()
    assert "ужми" not in llm.prompt_text()


async def test_an_oversized_summary_is_told_to_shrink(
    tmp_path: Path, storage: Storage, formatter: LineFormatter
) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100, summary_max_words=5)
    await fill(storage, 40)
    await storage.set_summary(CHAT, "раз два три четыре пять шесть семь", 0, 0)
    llm = FakeLLM("Короче")
    assert await make_summarizer(storage, llm, settings, formatter).update_once(CHAT)
    prompt = llm.prompt_text()
    assert "ТЕКУЩАЯ СВОДКА (слов: 7):\nраз два" in prompt
    assert prompt.endswith("не больше 5 слов, все четыре раздела. Текущая сводка длиннее лимита — ужми её.")


async def test_each_chunk_is_numbered_on_its_own(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    for i in range(1001, 1041):  # every message answers the previous one
        await storage.add_message(msg(i, f"ответ {i}", reply_to=i - 1))
    llm = FakeLLM("Сводка 1", "Сводка 2")
    summarizer = make_summarizer(storage, llm, settings, formatter, platform="Discord")
    assert await summarizer.update_once(CHAT)
    assert await summarizer.update_once(CHAT)

    first, second = chunk_lines(llm.prompt_text(0)), chunk_lines(llm.prompt_text(1))
    assert first[0].startswith("#1 ") and first[0].endswith(" ↩: ответ 1001")  # 1000 was never stored
    assert first[1].startswith("#2 ") and first[1].endswith(" ↩#1: ответ 1002")
    # The second chunk starts at #1 again; its first message answers one from the previous chunk.
    assert second[0].startswith("#1 ") and " ↩: " in second[0]
    assert second[1].startswith("#2 ") and " ↩#1: " in second[1]
    assert "долговременную память группового чата в Discord" in llm.prompt_text(1)


async def test_maintain_swallows_model_failures(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    await fill(storage, 40)
    summarizer = make_summarizer(storage, FakeLLM(LLMError("down")), settings, formatter)
    await summarizer.maintain(CHAT)
    assert await storage.get_summary(CHAT) is None


async def test_the_panel_picks_the_summary_models(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    await fill(storage, 40)
    bot_config = BotConfig(storage, settings)
    await bot_config.set_runtime(models=["panel/model"])
    llm = FakeLLM("Сводка")
    assert await Summarizer(storage, llm, settings, formatter, bot_config=bot_config).update_once(CHAT)  # type: ignore[arg-type]
    assert llm.options[0]["models"] == ["panel/model"]


async def test_the_summary_gets_room_and_a_cut_is_logged(
    tmp_path: Path, storage: Storage, formatter: LineFormatter, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100, summary_max_words=500)
    await fill(storage, 40)

    class CutLLM(FakeLLM):
        async def complete(self, messages: list[dict[str, str]], **options: object) -> Completion:
            completion = await super().complete(messages, **options)
            return dataclasses.replace(completion, finish_reason="length")

    llm = CutLLM("## Хроника\n- 20.09 Петя купил кроссовки\n## Участники\n- Петя обещал бегать по утр")
    assert await make_summarizer(storage, llm, settings, formatter).update_once(CHAT)
    assert llm.options[0]["max_tokens"] == 500 * 6
    assert "hit max_tokens" in caplog.text
    summary = await storage.get_summary(CHAT)  # the half-written last line is dropped
    assert summary is not None and summary.text == "## Хроника\n- 20.09 Петя купил кроссовки\n## Участники"
