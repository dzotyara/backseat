import re

from backseat.prompts import REACTION_EMOJIS
from backseat.replies import Action, clean_reply, parse_action, split_message

IDS = [10, 11, 12]


def test_skip_and_garbage_are_skips() -> None:
    assert parse_action("SKIP", IDS, REACTION_EMOJIS).kind == "skip"
    assert parse_action("User Safety: unsafe\nSafety Categories: Profanity", IDS, REACTION_EMOJIS).kind == "skip"
    assert parse_action("", IDS, REACTION_EMOJIS).kind == "skip"
    assert parse_action("REPLY #11\n", IDS, REACTION_EMOJIS).kind == "skip"  # nothing to say
    assert parse_action("REPLY #11\nтекст", [], REACTION_EMOJIS).kind == "skip"


def test_reply_on_the_next_lines() -> None:
    action = parse_action("REPLY #11\nКроссовки уже в отставке.\nВторая строка", IDS, REACTION_EMOJIS)
    assert action == Action("reply", 11, text="Кроссовки уже в отставке.\nВторая строка")


def test_reply_on_the_same_line_and_decorations() -> None:
    assert parse_action("REPLY #12 ну ты даёшь", IDS, REACTION_EMOJIS) == Action("reply", 12, text="ну ты даёшь")
    assert parse_action("**REPLY #10**\n«Классика»", IDS, REACTION_EMOJIS) == Action("reply", 10, text="Классика")
    fenced = "```\nREPLY #11\nтекст\n```"
    assert parse_action(fenced, IDS, REACTION_EMOJIS) == Action("reply", 11, text="текст")


def test_unknown_target_falls_back_to_the_last_message() -> None:
    assert parse_action("REPLY #999\nтекст", IDS, REACTION_EMOJIS).message_id == 12
    assert parse_action("REPLY\nтекст", IDS, REACTION_EMOJIS).message_id == 12


def test_reactions() -> None:
    assert parse_action("REACT #10 🤡", IDS, REACTION_EMOJIS) == Action("react", 10, emoji="🤡")
    assert parse_action("REACT #10 ❤️", IDS, REACTION_EMOJIS) == Action("react", 10, emoji="❤")
    assert parse_action("REACT #10 🦖", IDS, REACTION_EMOJIS).kind == "skip"
    assert parse_action("REACT #10 🤡", IDS, ()).kind == "skip"  # reactions disabled


def test_clean_reply() -> None:
    assert clean_reply("#12 14:05 Ты: сам такой") == "сам такой"
    assert clean_reply("Ты[42]: сам такой") == "сам такой"
    assert clean_reply('"в кавычках"') == "в кавычках"
    assert clean_reply("REPLY #3\nответ") == "ответ"
    assert clean_reply("```\nкод-блок\n```") == "код-блок"
    assert clean_reply("  обычный текст  ") == "обычный текст"


def test_split_message() -> None:
    text = "\n".join(["строка " * 20] * 50)
    chunks = split_message(text, limit=500)
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert re.sub(r"\s", "", "".join(chunks)) == re.sub(r"\s", "", text)  # only whitespace at cuts is lost
    assert split_message("коротко", limit=500) == ["коротко"]
    assert split_message("x" * 1200, limit=500) == ["x" * 500, "x" * 500, "x" * 200]
