from backseat.triggers import compile_names, is_trivial_text

NAMES = compile_names(["бэксит", "ботяра"])


def test_names_match_russian_case_endings() -> None:
    assert NAMES is not None
    for text in ("ботяра, ты тут?", "спроси ботяру", "Бэкситу привет", "эй БОТЯРА", "бэксита позовите"):
        assert NAMES.search(text), text


def test_names_do_not_match_inside_other_words() -> None:
    assert NAMES is not None
    for text in ("работяра пришёл", "бэкситянин", "ботинки", "бот"):
        assert not NAMES.search(text), text


def test_short_names_match_exactly() -> None:
    pattern = compile_names(["бот"])
    assert pattern is not None
    assert pattern.search("эй бот, ответь")
    assert not pattern.search("ботинок")


def test_no_names_means_no_pattern() -> None:
    assert compile_names(["", "  "]) is None


def test_trivial_texts() -> None:
    for text in ("ок", "Ага!", "ахахах))", "да)", "+", "😂😂", "лол", "ну да", "ХАХА"):
        assert is_trivial_text(text), text
    for text in ("да, я против", "кто в субботу?", "ахах, ну ты даёшь"):
        assert not is_trivial_text(text), text
