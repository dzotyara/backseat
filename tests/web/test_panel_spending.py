import sqlite3

from backseat.storage import LLMCall, ModerationEntry, Storage
from tests.web.conftest import CHAT, NOW, SECRETS, Panel, msg


def record(*calls: LLMCall):
    async def go(storage: Storage, config: object) -> None:
        for call in calls:
            await storage.add_llm_call(call)

    return go


def test_spending_page_adds_up_both_bots(panel: Panel) -> None:
    panel.telegram.setup()
    panel.discord.setup()
    panel.telegram.run(record(LLMCall(int(NOW) - 60, "answer", "m", "Relace", 1000, 750, 10, 0.0021, 1500)))
    panel.discord.run(
        record(
            LLMCall(int(NOW) - 120, "precheck", "m", "InferenceNet", 500, 0, 1, 0.0001, 800),
            LLMCall(int(NOW) - 3 * 86_400, "summary", "m", "InferenceNet", 5000, 0, 900, 0.002, 9000),
            LLMCall(int(NOW) - 60 * 86_400, "answer", "m", "Relace", cost=9.99),  # older than 30 days
        )
    )
    page = panel.client.get("/spending")
    assert page.status_code == 200
    text = page.text
    assert "$0.0042" in text  # 0.0021 + 0.0001 + 0.002, without the old $9.99
    assert "$9.99" not in text
    assert "Ответы" in text and "Предпроверки" in text and "Память и итоги недели" in text
    assert "Relace" in text and "InferenceNet" in text
    assert "<svg" in text and 'class="series-1"' in text
    assert "Учёт ведётся с" in text  # recording began within the period
    assert not any(secret in text for secret in SECRETS)
    assert "$0.0123" in text  # the key's own numbers are still there

    week = panel.client.get("/spending?days=7").text
    assert "За 7 дней" in week
    assert "За 30 дней" in panel.client.get("/spending?days=12345").text  # unknown periods fall back


def test_spending_page_without_bots_or_calls(panel: Panel) -> None:
    text = panel.client.get("/spending").text
    assert "За этот период вызовов нет" in text
    assert not panel.telegram.path.exists()  # the panel never creates a database


def test_spending_page_survives_a_broken_database(panel: Panel) -> None:
    panel.telegram.setup()
    panel.discord.path.write_bytes(b"not a database" * 100)
    page = panel.client.get("/spending")
    assert page.status_code == 200
    assert "Discord: база сейчас не читается" in page.text


def test_moderation_log_page(panel: Panel) -> None:
    panel.discord.setup(messages=[msg(1, "привет")])

    async def go(storage: Storage, config: object) -> None:
        await storage.add_moderation(
            ModerationEntry(int(NOW) - 60, CHAT, 700, "Иван (ivan)", "забань <b>Васю</b>", "✅ Вася: забанен")
        )

    empty = panel.client.get("/bots/discord/moderation").text
    assert "Пока пусто" in empty
    panel.discord.run(go)
    text = panel.client.get("/bots/discord/moderation").text
    assert "Иван (ivan)" in text and "✅ Вася: забанен" in text
    assert "&lt;b&gt;Васю&lt;/b&gt;" in text  # escaped
    assert "/bots/discord/moderation" in panel.client.get("/bots/discord/persona").text  # the tab
    panel.telegram.setup()
    assert "/bots/telegram/moderation" not in panel.client.get("/bots/telegram/persona").text


def test_old_databases_get_the_new_tables(panel: Panel) -> None:
    # A database from before v2.2: the panel opening it must not fail on the missing tables.
    with sqlite3.connect(panel.telegram.path) as db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    assert panel.client.get("/spending").status_code == 200
