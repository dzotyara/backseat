from backseat.bot_config import parse_names
from backseat.commands import MAX_PROMPT_FILE_BYTES, decode_prompt_file, is_prompt_file, is_reset


def test_reset_words() -> None:
    assert is_reset(" Сброс ") and is_reset("reset") and is_reset("DEFAULT")
    assert not is_reset(None) and not is_reset("") and not is_reset("сбрось всё")


def test_prompt_file_must_be_small_text() -> None:
    assert is_prompt_file("text/plain", "persona", 10)
    assert is_prompt_file(None, "Persona.MD", None)  # Telegram may not know the type or the size
    assert is_prompt_file("application/octet-stream", "persona.txt", MAX_PROMPT_FILE_BYTES)
    assert not is_prompt_file("application/octet-stream", "persona.txt", MAX_PROMPT_FILE_BYTES + 1)
    assert not is_prompt_file("image/png", "persona.png", 10)


def test_prompt_file_encodings() -> None:
    assert decode_prompt_file("﻿Ты — бард.\n".encode()) == "Ты — бард."
    assert decode_prompt_file("Ты — бард.".encode("cp1251")) == "Ты — бард."  # Notepad on a Russian Windows
    assert decode_prompt_file(b"  \n") is None


def test_names_as_the_owner_types_them() -> None:
    assert parse_names(" бэксит, ботяра;бот\nботяра ,, ") == ["бэксит", "ботяра", "бот"]
    assert parse_names(" ;, ") == []
