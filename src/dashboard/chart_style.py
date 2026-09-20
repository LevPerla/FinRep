"""Shared color rules for categorical dashboard charts."""

from hashlib import sha256


# Medium, muted colors remain readable on both dashboard themes.
CATEGORY_PALETTE = (
    "#7899BC", "#BB88A4", "#83AD91", "#BD9A71", "#A38DB9",
    "#79AFB1", "#B7AD75", "#B58C83", "#8794A5", "#96A778",
)
EXPENSE_CATEGORY_COLORS = dict(zip((
    "Быт и товары для дома", "Жилье", "На себя", "Одежда", "Пища",
    "Поездки", "Прочее", "Связь", "Соц.жизнь", "Транспорт",
), CATEGORY_PALETTE))
INCOME_SOURCE_COLORS = {
    "salary": "#7899BC",
    "deposit_interest": "#83AD91",
    "unknown": "#8794A5",
    "savings": "#B7AD75",
}


def expense_category_color(category: str) -> str:
    key = str(category).replace("ё", "е")
    if key in EXPENSE_CATEGORY_COLORS:
        return EXPENSE_CATEGORY_COLORS[key]
    return CATEGORY_PALETTE[int.from_bytes(sha256(key.encode("utf-8")).digest()[:2], "big") % len(CATEGORY_PALETTE)]
