from datetime import time
from pathlib import Path
from typing import Annotated, Any

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_MODELS = [
    "deepseek/deepseek-v4.1-flash",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


def _split(value: Any) -> Any:
    """Comma-separated env values -> list; lists pass through untouched."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class CoreSettings(BaseSettings):
    """Everything the platform-independent core needs; each platform adds its own token."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `FOO=` in .env means "use the default", not ""
        extra="ignore",  # old v1 variables (CONTEXT_WINDOW, GONKAGATE_*) are harmless
        validate_by_name=True,
        validate_by_alias=True,
    )

    openrouter_api_key: SecretStr
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_name: str = "Backseat"
    openrouter_site_url: str = ""

    # Tried in order: the first one that answers wins. MODEL_NAME is the v1 name.
    models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_MODELS),
        validation_alias=AliasChoices("MODELS", "MODEL_NAME"),
    )
    # off | low | medium | high — chat replies don't need a model's hidden reasoning.
    reasoning: str = "off"
    # OpenRouter hosts to try first, e.g. "InferenceNet,Relace". A preference, not a filter: the rest stay
    # as a fallback. Empty = OpenRouter picks, and it picked hosts 6-8x pricier than the cheapest.
    providers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_tokens: int = 1000
    request_timeout_seconds: float = 60.0

    owner_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Telegram group ids or Discord channel ids. Empty = any chat.
    # Set it so strangers can't add the bot and spend your credits.
    allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Discord user ids allowed to rename members and manage roles by asking the bot in the chat.
    moderator_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    bot_names: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["бэксит", "ботяра"])
    # "123456789:Иван,987654321:Петя" — fixed display name and a longer personal history.
    focus_users: Annotated[dict[int, str], NoDecode] = Field(default_factory=dict)

    persona_file: Path = Path("prompts/persona.md")
    db_path: Path = Path("data/backseat.db")
    timezone: str = "Europe/Moscow"

    debounce_seconds: float = 5.0
    addressed_debounce_seconds: float = 1.5
    max_batch_wait_seconds: float = 20.0
    max_batch_messages: int = 15
    unprompted_cooldown_seconds: float = 60.0
    # After the bot answers someone, that person waits this long: their call meanwhile gets a canned
    # "not ready yet" (once) instead of a paid answer. Everyone has their own timer. 0 = off.
    reply_freeze_seconds: float = 30.0
    # Before a comment nobody asked for, first show the model only this many tokens of the latest chat and
    # ask yes/no; the full prompt with the chat's memory is paid for only on "yes". 0 = always the full prompt.
    precheck_context_tokens: int = 0

    # Rough token budgets for the prompt sections (1 token ~ 3 characters).
    recent_context_tokens: int = 5000
    focus_history_tokens: int = 1500
    author_history_tokens: int = 600
    summary_chunk_tokens: int = 2500
    summary_max_words: int = 600
    # Off: no summary at all — the bot knows only the recent window (and ЧТО ПИСАЛИ РАНЬШЕ, if its budgets > 0).
    summary_enabled: bool = True
    # The service rules before the persona, with {platform} {names} {username} {participants} {persona}.
    # Empty = the built-in prompts.SYSTEM_TEMPLATE.
    system_template: str = ""

    # Pictures on request («нарисуй…»): drawn for free by Pollinations, one at a time; the paid image
    # models (OpenRouter) only as a fallback and at most PAID_IMAGES_PER_DAY a day (0 = never pay).
    images_enabled: bool = True
    # A Pollinations key (enter.pollinations.ai) unlocks their better models, paid from its pollen balance
    # (Z-Image Turbo ≈ $0.004 a picture). Without it, or when the balance is out, the free anonymous Sana.
    pollinations_api_key: SecretStr | None = None
    # Cloudflare Workers AI (dash.cloudflare.com, token from the "Workers AI" template): tried first, free
    # within its daily allocation (~$0.0006 a picture after that); the day it runs out, Pollinations draws.
    cloudflare_account_id: str = ""
    cloudflare_api_token: SecretStr | None = None
    cloudflare_image_model: str = "@cf/black-forest-labs/flux-1-schnell"
    pollinations_models: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["zimage", "flux"])
    images_per_user_per_day: int = 0  # 0 = no limit
    # square | landscape (16:9) | portrait (9:16), unless the request says otherwise; and the long side in px.
    # Cloudflare's FLUX.1 Schnell draws only 1024×1024 squares: other shapes go to Pollinations.
    # Off: Cloudflare only (free; squares only; when its allocation is spent, the bot says when it renews).
    # On: then Pollinations with a key, then the anonymous Sana.
    image_fallbacks: bool = False
    image_shape: str = "square"
    image_size: int = 1024
    paid_images_per_day: int = 0
    image_models: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["openai/gpt-5-image-mini"])

    reactions_enabled: bool = True
    weekly_digest: bool = True
    digest_weekday: int = Field(default=6, ge=0, le=6)  # Monday=0 ... Sunday=6
    digest_time: time = time(20, 0)
    digest_min_messages: int = 20

    log_level: str = "INFO"

    @field_validator("models", "providers", "bot_names", "image_models", "pollinations_models", mode="before")
    @classmethod
    def _parse_str_list(cls, value: Any) -> Any:
        return _split(value)

    @field_validator("owner_ids", "allowed_chat_ids", "moderator_ids", mode="before")
    @classmethod
    def _parse_int_list(cls, value: Any) -> Any:
        value = _split(value)
        if isinstance(value, list):
            return [int(item) for item in value]
        return value

    @field_validator("focus_users", mode="before")
    @classmethod
    def _parse_focus_users(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        users = {}
        for part in _split(value):
            user_id, _, name = part.partition(":")
            users[int(user_id)] = name.strip() or user_id.strip()
        return users

    @field_validator("reasoning")
    @classmethod
    def _check_reasoning(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"off", "low", "medium", "high"}:
            raise ValueError("REASONING must be one of: off, low, medium, high")
        return value
