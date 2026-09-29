"""What the owner typed into the panel's forms, parsed and checked. Error messages go to the page, so
they are in Russian."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from backseat.bot_config import BUDGET_RANGE, IMAGES_RANGE, MAX_TOKENS_RANGE, Runtime, parse_names
from backseat.prompts import SYSTEM_PLACEHOLDERS
from backseat.web.formatting import format_seconds, number

MAX_PERSONA_CHARS = 100_000

_MODEL_RE = re.compile(r"[^/\s]+/\S+")
_SPLIT_RE = re.compile(r"[\s,;]+")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One Runtime field on the settings page."""

    name: str
    kind: str  # flag | seconds | integer | models | words | ids | text
    label: str
    hint: str
    group: str
    platforms: tuple[str, ...] = ()  # the bots that have it; empty = both
    low: int = 0
    high: int = 0


GROUPS = ("Где и для кого", "Модель", "Когда говорить", "Что бот помнит", "Картинки", "Системный промпт")

SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "allowed_chat_ids",
        "ids",
        "Чаты и каналы",
        "ID через запятую или с новой строки. Бот читает и отвечает только там; в Discord треды внутри "
        "канала тоже считаются. Пусто — любой чат, куда бота добавили.",
        GROUPS[0],
    ),
    FieldSpec(
        "moderator_ids",
        "ids",
        "Модераторы",
        "Discord ID тех, кто может попросить бота в чате поменять ник, создать роль, выдать её или сменить "
        "цвет: «ботяра, поменяй Ивокси ник на Антон, роль Морпех, цвет хаки». Остальных такие просьбы не касаются.",
        GROUPS[0],
        platforms=("Discord",),
    ),
    FieldSpec(
        "models",
        "models",
        "Модели",
        "По одной в строке, в порядке очереди: отвечает первая, которая смогла, остальные — запасные. "
        "Названия — как на openrouter.ai/models.",
        GROUPS[1],
    ),
    FieldSpec(
        "providers",
        "words",
        "Хостинги",
        "К кому из хостингов модели идти в первую очередь, через запятую; остальные остаются запасными. "
        "Пусто — выбирает OpenRouter (часто дороже).",
        GROUPS[1],
    ),
    FieldSpec(
        "max_tokens",
        "integer",
        "Максимум токенов на ответ",
        "Потолок длины одного ответа или комментария. Обычно бот пишет коротко сам; это страховка от простыни.",
        GROUPS[1],
        low=MAX_TOKENS_RANGE[0],
        high=MAX_TOKENS_RANGE[1],
    ),
    FieldSpec(
        "unprompted_cooldown_seconds",
        "seconds",
        "Пауза между комментариями, секунд",
        "Сколько бот выжидает после своего сообщения, прежде чем снова влезть в разговор без спроса. "
        "На обращения отвечает всегда.",
        GROUPS[2],
    ),
    FieldSpec(
        "reply_freeze_seconds",
        "seconds",
        "Пауза перед новым ответом тому же человеку, секунд",
        "Кто только что получил ответ, до конца паузы получает «ещё не готов» вместо нового платного ответа. "
        "0 — без паузы.",
        GROUPS[2],
    ),
    FieldSpec(
        "precheck_context_tokens",
        "integer",
        "Предпроверка, токенов",
        "Прежде чем влезть без спроса, бот дёшево спрашивает модель «есть ли повод?» по последним строкам "
        "такого объёма. 0 — сразу полный запрос со всей памятью (дороже в оживлённом чате).",
        GROUPS[2],
        low=BUDGET_RANGE[0],
        high=BUDGET_RANGE[1],
    ),
    FieldSpec(
        "reactions_enabled",
        "flag",
        "Реакции эмодзи",
        "Иногда вместо комментария бот просто ставит реакцию на сообщение.",
        GROUPS[2],
    ),
    FieldSpec(
        "weekly_digest",
        "flag",
        "Итоги недели",
        "Раз в неделю бот публикует в активных чатах пост «Итоги недели».",
        GROUPS[2],
    ),
    FieldSpec(
        "recent_context_tokens",
        "integer",
        "Последняя переписка, токенов",
        "Сколько свежих сообщений бот видит дословно (1 токен ≈ 3 символа). Больше — лучше помнит разговор, "
        "но каждый ответ дороже.",
        GROUPS[3],
        low=BUDGET_RANGE[0],
        high=BUDGET_RANGE[1],
    ),
    FieldSpec(
        "summary_enabled",
        "flag",
        "Сводка давней переписки",
        "Бот сворачивает то, что выпало из последней переписки, в сжатую сводку и помнит события неделями. "
        "Выключено — знает только последнюю переписку.",
        GROUPS[3],
    ),
    FieldSpec(
        "author_history_tokens",
        "integer",
        "Старые сообщения автора, токенов",
        "Прошлые реплики того, кто пишет сейчас, — для «а неделю назад ты говорил обратное». 0 — не подтягивать.",
        GROUPS[3],
        low=BUDGET_RANGE[0],
        high=BUDGET_RANGE[1],
    ),
    FieldSpec(
        "focus_history_tokens",
        "integer",
        "Старые сообщения постоянных участников, токенов",
        "То же для участников из FOCUS_USERS — их прошлое бот подтягивает всегда. 0 — не подтягивать.",
        GROUPS[3],
        low=BUDGET_RANGE[0],
        high=BUDGET_RANGE[1],
    ),
    FieldSpec(
        "images_enabled",
        "flag",
        "Рисовать картинки",
        "«ботяра, нарисуй кота в танке» — бот рисует через Pollinations, по одной картинке за раз "
        "(остальные ждут в очереди).",
        GROUPS[4],
    ),
    FieldSpec(
        "pollinations_models",
        "words",
        "Модели Pollinations",
        "Работают, если на сервере задан POLLINATIONS_API_KEY; платятся из баланса pollen ключа. По порядку: "
        "первая — основная. zimage ≈ $0.004 за картинку, flux ≈ $0.002, klein ≈ $0.005. Без ключа, при пустом "
        "балансе или пустом списке — бесплатная, но слабая Sana.",
        GROUPS[4],
    ),
    FieldSpec(
        "images_per_user_per_day",
        "integer",
        "Картинок в день на человека",
        "Сколько картинок один человек может заказать за сутки. 0 — без ограничений.",
        GROUPS[4],
        low=IMAGES_RANGE[0],
        high=IMAGES_RANGE[1],
    ),
    FieldSpec(
        "paid_images_per_day",
        "integer",
        "Платных картинок в день",
        "Если бесплатная рисовалка не ответила, бот может нарисовать платной моделью (≈ $0.01 за картинку), "
        "но не больше стольких раз в сутки на весь чат. 0 — никогда не платить: бот скажет «попробуй позже».",
        GROUPS[4],
        low=IMAGES_RANGE[0],
        high=IMAGES_RANGE[1],
    ),
    FieldSpec(
        "image_models",
        "models",
        "Платные модели картинок",
        "По одной в строке, первая — основная. Работают только при платных картинках больше 0.",
        GROUPS[4],
    ),
    FieldSpec(
        "system_template",
        "text",
        "Служебные правила",
        "Что бот знает о формате переписки и как себя вести, до раздела «Характер». Подстановки: "
        + ", ".join("{" + name + "}" for name in SYSTEM_PLACEHOLDERS)
        + ". Сам характер — на вкладке «Характер».",
        GROUPS[4],
    ),
)


def specs_for(platform: str) -> tuple[FieldSpec, ...]:
    return tuple(spec for spec in SPECS if not spec.platforms or platform in spec.platforms)


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


def split_models(text: str) -> list[str]:
    return list(dict.fromkeys(model for model in _SPLIT_RE.split(text) if model))


def show_value(spec: FieldSpec, value: Any) -> str:
    """A Runtime value as it goes into the form field."""
    match spec.kind:
        case "seconds":
            return format_seconds(value)
        case "integer":
            return str(value)
        case "models":
            return "\n".join(value)
        case "words" | "ids":
            return ", ".join(str(item) for item in value)
        case "text":
            return value
    return ""


def _parse(spec: FieldSpec, raw: str) -> tuple[Any, str | None]:
    """(value, error) for one non-flag field."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    match spec.kind:
        case "seconds":
            try:
                seconds = float(raw.strip().replace(",", "."))
            except ValueError:
                return None, "Нужно число секунд, например 60."
            if not math.isfinite(seconds):
                return None, "Нужно обычное число секунд, например 60."
            if seconds < 0:
                return None, "Пауза не может быть отрицательной."
            return seconds, None
        case "integer":
            try:
                value = int(raw.strip().replace(" ", ""))
            except ValueError:
                return None, f"Нужно целое число от {number(spec.low)} до {number(spec.high)}."
            if not spec.low <= value <= spec.high:
                return None, f"Нужно целое число от {number(spec.low)} до {number(spec.high)}."
            return value, None
        case "models":
            models = split_models(raw)
            if not models:
                return None, "Нужна хотя бы одна модель."
            typo = next((model for model in models if not _MODEL_RE.fullmatch(model)), None)
            if typo is not None:
                return None, f"«{typo}» не похоже на модель OpenRouter: нужен вид автор/модель."
            return models, None
        case "words":
            return split_models(raw), None
        case "ids":
            ids = []
            for item in _SPLIT_RE.split(raw.strip()):
                if not item:
                    continue
                if not re.fullmatch(r"-?\d{1,20}", item):
                    return None, f"«{item}» — не ID: нужны только цифры (у групп Telegram — с минусом)."
                ids.append(int(item))
            return list(dict.fromkeys(ids)), None
        case "text":
            text = raw.strip()
            if not text:
                return None, "Текст не может быть пустым. Чтобы вернуть стандартный, нажмите «вернуть по умолчанию»."
            if len(text) > MAX_PERSONA_CHARS:
                return None, f"Слишком длинно: можно не больше {number(MAX_PERSONA_CHARS)} символов."
            return text, None
    raise ValueError(f"unknown field kind: {spec.kind}")


@dataclass
class SettingsForm:
    """The settings page: the text as typed (shown back after an error) and the parsed values. Fields
    missing from a POST are left as they are; a flag's hidden "0" tells an unchecked box from a missing one."""

    specs: tuple[FieldSpec, ...]
    names_text: str
    texts: dict[str, str] = field(default_factory=dict)  # non-flag fields as typed
    flags: dict[str, bool] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)  # parsed, only the submitted fields
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def show(cls, specs: tuple[FieldSpec, ...], names: list[str], runtime: Runtime) -> Self:
        form = cls(specs=specs, names_text=", ".join(names), names=names)
        for spec in specs:
            value = getattr(runtime, spec.name)
            if spec.kind == "flag":
                form.flags[spec.name] = value
            else:
                form.texts[spec.name] = show_value(spec, value)
        return form

    @classmethod
    def parse(cls, specs: tuple[FieldSpec, ...], form: Any) -> Self:
        parsed = cls(specs=specs, names_text=text_field(form, "names"))
        parsed.names = parse_names(parsed.names_text)
        if not parsed.names:
            parsed.errors["names"] = "Нужно хотя бы одно имя."
        for spec in specs:
            submitted = form.getlist(spec.name) if hasattr(form, "getlist") else [form[spec.name]]
            if spec.name not in form:
                continue
            if spec.kind == "flag":
                parsed.flags[spec.name] = parsed.values[spec.name] = "1" in submitted
                continue
            raw = submitted[-1] if submitted and isinstance(submitted[-1], str) else ""
            parsed.texts[spec.name] = raw
            value, error = _parse(spec, raw)
            if error:
                parsed.errors[spec.name] = error
            else:
                parsed.values[spec.name] = value
        return parsed

    def grouped(self) -> list[tuple[str, list[FieldSpec]]]:
        return [(group, [spec for spec in self.specs if spec.group == group]) for group in GROUPS]
