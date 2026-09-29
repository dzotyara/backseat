import json

import pytest

from tests.web.conftest import BOT_MODELS, PERSONA, Panel


def test_pause_and_resume(panel: Panel) -> None:
    panel.telegram.setup()
    page = panel.client.post("/bots/telegram/pause", data={"paused": "1", "next": "/bots/telegram/memory"})
    assert page.url.path == "/bots/telegram/memory"
    assert "Telegram: пауза. Бот читает и запоминает чат, но ничего не пишет." in page.text
    assert "на паузе" in page.text
    assert panel.telegram.runtime().paused
    assert "Telegram: пауза" not in panel.client.get("/").text  # a flash message is shown once

    page = panel.client.post("/bots/telegram/pause", data={"paused": "0"})
    assert page.url.path == "/"
    assert "Telegram: бот снова пишет в чат." in page.text
    assert not panel.telegram.runtime().paused
    assert panel.telegram.setting("runtime") is None  # not paused is the default: nothing stored


def test_pause_redirects_only_within_the_panel(panel: Panel) -> None:
    panel.telegram.setup()
    for target in ("//evil.example/x", "https://evil.example/", "/\\evil.example"):
        response = panel.client.post(
            "/bots/telegram/pause", data={"paused": "1", "next": target}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/"


def test_missing_bots_are_404_and_nothing_is_created(panel: Panel) -> None:
    page = panel.client.get("/bots/discord/persona")
    assert page.status_code == 404 and "Discord-бот ещё не настроен" in page.text
    assert panel.client.post("/bots/discord/pause", data={"paused": "1"}).status_code == 404
    assert panel.client.post("/bots/discord/persona", data={"persona": "текст"}).status_code == 404
    assert panel.client.get("/bots/whatsapp/settings").status_code == 404
    assert panel.client.get("/nowhere").status_code == 404
    assert not panel.discord.path.exists()


def test_persona_save_and_reset_with_confirmation(panel: Panel) -> None:
    panel.telegram.setup()
    page = panel.client.get("/bots/telegram/persona").text
    assert "Стеби Ивана." in page and "Сбросить к стандартному" not in page

    page = panel.client.post("/bots/telegram/persona", data={"persona": "  Новый характер.\r\nВторая строка.  "}).text
    assert "Характер сохранён (30 символов)" in page
    assert panel.telegram.setting("persona") == "Новый характер.\nВторая строка."
    assert "Сбросить к стандартному" in page

    confirm = panel.client.get("/bots/telegram/persona/reset").text
    assert "Вернуть характер по умолчанию?" in confirm and "Стеби Ивана." in confirm
    assert panel.telegram.setting("persona") is not None  # asking is not resetting

    page = panel.client.post("/bots/telegram/persona/reset").text
    assert "Вернул характер по умолчанию." in page
    assert panel.telegram.setting("persona") is None
    page = panel.client.get("/bots/telegram/persona/reset").text  # nothing left to reset
    assert "Бот и так использует характер по умолчанию." in page


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (" \r\n ", "Характер не может быть пустым."),
        ("х" * 100_001, "Слишком длинно: можно не больше 100 000 символов, а здесь 100 001."),
    ],
    ids=["empty", "too-long"],  # the ids end up in an environment variable: keep them short
)
def test_persona_validation(panel: Panel, text: str, error: str) -> None:
    panel.telegram.setup()
    page = panel.client.post("/bots/telegram/persona", data={"persona": text})
    assert page.status_code == 400
    assert error in page.text
    assert panel.telegram.setting("persona") is None


def test_persona_equal_to_the_default_is_not_stored(panel: Panel) -> None:
    panel.telegram.setup()
    page = panel.client.post("/bots/telegram/persona", data={"persona": PERSONA.replace("\n", "\r\n") + "\r\n"})
    assert "совпадает со стандартным" in page.text
    assert panel.telegram.setting("persona") is None


def test_behaviour_is_saved_as_overrides_only(panel: Panel) -> None:
    panel.telegram.setup()
    page = panel.client.get("/bots/telegram/settings").text
    assert 'value="бэксит, ботяра"' in page and "\n".join(BOT_MODELS) in page
    defaults_shown = page.count('class="chip chip-muted">по умолчанию')
    assert defaults_shown == 22  # names + 21 settings (no moderators: that is Discord's)

    form = {
        "names": "Железяка; бот,, бот",
        "models": "vendor/new-model\npaid/model, vendor/new-model",
        "unprompted_cooldown_seconds": "90,5",
        "weekly_digest": ["0", "1"],  # every checkbox comes with a hidden "0"
        "reactions_enabled": "0",  # unchecked: only the hidden "0"
        "max_tokens": "300",
        "allowed_chat_ids": "-100123, 456\n-100123",
    }
    page = panel.client.post("/bots/telegram/settings", data=form).text
    assert "Сохранено." in page
    runtime = panel.telegram.runtime()
    assert runtime.models == ["vendor/new-model", "paid/model"]
    assert runtime.unprompted_cooldown_seconds == 90.5
    assert (runtime.reactions_enabled, runtime.weekly_digest, runtime.paused) == (False, True, False)
    assert json.loads(panel.telegram.setting("names") or "") == ["Железяка", "бот"]
    assert runtime.max_tokens == 300 and runtime.allowed_chat_ids == [-100123, 456]
    assert json.loads(panel.telegram.setting("runtime") or "") == {
        "models": ["vendor/new-model", "paid/model"],
        "unprompted_cooldown_seconds": 90.5,
        "reactions_enabled": False,
        "max_tokens": 300,
        "allowed_chat_ids": [-100123, 456],
    }  # weekly_digest equals the default, and fields not sent stay as they are
    assert page.count('class="chip chip-accent">изменено') == 6


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("names", " , ;", "Нужно хотя бы одно имя."),
        ("models", " \n ", "Нужна хотя бы одна модель."),
        ("models", "paid/model\ndeepseek", "«deepseek» не похоже на модель OpenRouter"),
        ("unprompted_cooldown_seconds", "-5", "Пауза не может быть отрицательной."),
        ("unprompted_cooldown_seconds", "минута", "Нужно число секунд, например 60."),
        ("unprompted_cooldown_seconds", "inf", "Нужно обычное число секунд"),
        ("max_tokens", "5", "Нужно целое число от 16 до 16"),
        ("allowed_chat_ids", "123, чат", "«чат» — не ID"),
        ("system_template", " \r\n ", "Текст не может быть пустым."),
    ],
)
def test_behaviour_validation(panel: Panel, field: str, value: str, error: str) -> None:
    panel.telegram.setup()
    form = {"names": "бэксит", "models": "a/b", "unprompted_cooldown_seconds": "60", field: value}
    page = panel.client.post("/bots/telegram/settings", data=form)
    assert page.status_code == 400
    assert "Не сохранено" in page.text and error in page.text
    assert panel.telegram.setting("runtime") is None and panel.telegram.setting("names") is None


def test_behaviour_reset_one_field_or_everything(panel: Panel) -> None:
    panel.telegram.setup()
    panel.telegram.run(lambda storage, config: config.set_runtime(paused=True, models=["x/y"], reactions_enabled=False))
    panel.telegram.run(lambda storage, config: config.set_names(["железяка"]))

    page = panel.client.post("/bots/telegram/settings/reset", data={"field": "models"}).text
    assert "Вернул по умолчанию: модели." in page
    runtime = panel.telegram.runtime()
    assert runtime.models == BOT_MODELS and runtime.reactions_enabled is False

    page = panel.client.post("/bots/telegram/settings/reset", data={"field": "all"}).text
    assert "Все настройки — снова по умолчанию." in page
    runtime = panel.telegram.runtime()
    assert runtime.reactions_enabled is True
    assert runtime.paused is True  # the pause belongs to the switch, not to this form
    assert panel.telegram.setting("names") is None

    page = panel.client.post("/bots/telegram/settings/reset", data={"field": "paused"}).text
    assert "Непонятно, что сбросить." in page
    assert panel.telegram.runtime().paused is True
