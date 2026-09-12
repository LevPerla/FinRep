from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


MONEY_QUANTUM = Decimal("0.01")
_TEXT_AMOUNT_RE = re.compile(
    r"^[+-]?(?:(?:\d+|\d{1,3}(?:[ \u00a0]\d{3})+)(?:[.,]\d+)?|[.,]\d+)$"
)


def parse_money_amount(value, *, field_name: str = "amount") -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite monetary amount")

    if isinstance(value, str):
        text = value.strip()
        if not text or not _TEXT_AMOUNT_RE.fullmatch(text):
            raise ValueError(f"{field_name} has an invalid or ambiguous monetary format")
        normalized = text.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    else:
        normalized = str(value)

    try:
        amount = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite monetary amount") from exc
    if not amount.is_finite():
        raise ValueError(f"{field_name} must be a finite monetary amount")
    return amount


def quantize_money_amount(value, *, field_name: str = "amount") -> Decimal:
    amount = parse_money_amount(value, field_name=field_name)
    integer_digits = max(amount.adjusted() + 1, 1) if amount else 1
    precision = max(28, len(amount.as_tuple().digits), integer_digits + 2)
    with localcontext() as context:
        context.prec = precision
        return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def format_money_amount(
    value,
    *,
    decimal_separator: str = ".",
    field_name: str = "amount",
) -> str:
    if decimal_separator not in {".", ","}:
        raise ValueError("decimal_separator must be '.' or ','")
    amount = quantize_money_amount(value, field_name=field_name)
    if amount == 0:
        amount = Decimal("0")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", decimal_separator)
