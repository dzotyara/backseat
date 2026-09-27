from pathlib import Path

from backseat.llm import LLMError
from backseat.render import LineFormatter
from backseat.storage import Storage
from backseat.summarizer import Summarizer
from tests.conftest import CHAT, FakeLLM, make_settings, msg


async def fill(storage: Storage, count: int) -> None:
    for i in range(1, count + 1):
        await storage.add_message(msg(i, f"сообщение номер {i} про планы на выходные"))


async def test_short_history_is_not_summarized(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path)
    await fill(storage, 20)
    llm = FakeLLM()
    assert not await Summarizer(storage, llm, settings, formatter).update_once(CHAT)  # type: ignore[arg-type]
    assert llm.calls == []


async def test_oldest_chunk_is_folded_into_the_summary(
    tmp_path: Path, storage: Storage, formatter: LineFormatter
) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    await fill(storage, 40)
    llm = FakeLLM("Сводка 1", "Сводка 2")
    summarizer = Summarizer(storage, llm, settings, formatter)  # type: ignore[arg-type]

    assert await summarizer.update_once(CHAT)
    first = await storage.get_summary(CHAT)
    assert first is not None and first.text == "Сводка 1"
    prompt = llm.prompt_text()
    assert "ТЕКУЩАЯ СВОДКА:\n(пока пусто)" in prompt
    assert "#1 " in prompt and "#40 " not in prompt
    assert "Про Иван пиши подробнее всех" in prompt
    assert 1 < first.upto_message_id < 40

    assert await summarizer.update_once(CHAT)
    second = await storage.get_summary(CHAT)
    assert second is not None and second.upto_message_id > first.upto_message_id
    assert "ТЕКУЩАЯ СВОДКА:\nСводка 1" in llm.prompt_text()


async def test_maintain_swallows_model_failures(tmp_path: Path, storage: Storage, formatter: LineFormatter) -> None:
    settings = make_settings(tmp_path, recent_context_tokens=100, summary_chunk_tokens=100)
    await fill(storage, 40)
    summarizer = Summarizer(storage, FakeLLM(LLMError("down")), settings, formatter)  # type: ignore[arg-type]
    await summarizer.maintain(CHAT)
    assert await storage.get_summary(CHAT) is None
