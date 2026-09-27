"""What the owner typed into the panel's forms, parsed and checked. Error messages go to the page, so
they are in Russian."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from backseat.bot_config import Runtime
from backseat.web.formatting import format_seconds, number

MAX_PERSONA_CHARS = 100_000
# Runtime fields on the behaviour form; "paused" is the switch, not part of it.
RUNTIME_FIELDS = ("models", "unprompted_cooldown_seconds", "reactions_enabled", "weekly_digest")
BEHAVIOUR_FIELDS = ("names", *RUNTIME_FIELDS)  # each can be reset to its default on its own

_MODEL_RE = re.compile(r"[^/\s]+/\S+")


def text_field(form: Mapping[str, Any], name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def clean_persona(raw: str) -> tuple[str, str | None]:
    """(normalized text, error). Browsers send CRLF line endings; the persona file has LF."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return text, "Характер не может быть пустым. Чтобы вернуть стандартный, нажмите «Сбросить к стандартному»."
    if len(text) > MAX_PERSONA_CHARS:
        limit, size = number(MAX_PERSONA_CHARS), number(len(text))
        return text, f"Слишком длинно: можно не больше {limit} символов, а здесь {size}."
    return text, None


def split_names(text: str) -> list[str]:
    return list(dict.fromkeys(name.strip() for name in re.split(r"[,;\n]+", text) if name.strip()))


def split_models(text: str) -> list[str]:
    return list(dict.fromkeys(model for model in re.split(r"[\s,;]+", text) if model))


@dataclass
class Behaviour:
    """The behaviour form: the text as typed (shown back after an error) and the parsed values."""

    names_text: str
    models_text: str
    cooldown_text: str
    reactions_enabled: bool
    weekly_digest: bool
    names: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    cooldown: float = 0.0
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def show(cls, names: list[str], runtime: Runtime) -> Self:
        return cls(
            names_text=", ".join(names),
            models_text="\n".join(runtime.models),
            cooldown_text=format_seconds(runtime.unprompted_cooldown_seconds),
            reactions_enabled=runtime.reactions_enabled,
            weekly_digest=runtime.weekly_digest,
            names=names,
            models=runtime.models,
            cooldown=runtime.unprompted_cooldown_seconds,
        )

    @classmethod
    def parse(cls, form: Mapping[str, Any]) -> Self:
        behaviour = cls(
            names_text=text_field(form, "names"),
            models_text=text_field(form, "models"),
            cooldown_text=text_field(form, "unprompted_cooldown_seconds").strip(),
            reactions_enabled="reactions_enabled" in form,  # an unchecked box is not sent at all
            weekly_digest="weekly_digest" in form,
        )
        behaviour._check()
        return behaviour

    def _check(self) -> None:
        self.names = split_names(self.names_text)
        if not self.names:
            self.errors["names"] = "Нужно хотя бы одно имя."

        self.models = split_models(self.models_text)
        typo = next((model for model in self.models if not _MODEL_RE.fullmatch(model)), None)
        if not self.models:
            self.errors["models"] = "Нужна хотя бы одна модель."
        elif typo is not None:
            self.errors["models"] = (
                f"«{typo}» не похоже на модель OpenRouter: нужен вид автор/модель, "
                "например deepseek/deepseek-v4.1-flash."
            )

        try:
            self.cooldown = float(self.cooldown_text.replace(",", "."))
        except ValueError:
            self.errors["unprompted_cooldown_seconds"] = "Нужно число секунд, например 60."
            return
        if not math.isfinite(self.cooldown):
            self.errors["unprompted_cooldown_seconds"] = "Нужно обычное число секунд, например 60."
        elif self.cooldown < 0:
            self.errors["unprompted_cooldown_seconds"] = "Пауза не может быть отрицательной."
