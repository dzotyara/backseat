"""Numbers, money and time spans the Russian way, for the pages."""


def plural(count: int, one: str, few: str, many: str) -> str:
    """1 сообщение, 2 сообщения, 5 сообщений."""
    count = abs(count) % 100
    if 11 <= count <= 19:
        return many
    count %= 10
    return one if count == 1 else few if 2 <= count <= 4 else many


def number(value: int) -> str:
    return f"{value:,}".replace(",", " ")  # 12 345, with a narrow no-break space


def money(value: float) -> str:
    return f"${value:.2f}" if value == 0 or value >= 1 else f"${value:.4f}"


def axis_money(value: float) -> str:
    """$0, $0.01, $0.025 — an axis label needs no trailing zeros."""
    return "$" + (f"{value:.4f}".rstrip("0").rstrip(".") or "0")


def percent(share: float) -> str:
    return f"{share:.0%}" if share >= 0.01 or share == 0 else "<1%"


def format_seconds(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def ago(elapsed: float) -> str:
    if elapsed < 60:
        return "только что"
    minutes = int(elapsed // 60)
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} {plural(days, 'день', 'дня', 'дней')} назад"
