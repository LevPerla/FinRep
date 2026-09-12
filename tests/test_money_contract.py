from decimal import Decimal

import pandas as pd
import pytest

from src import config
from src.data import staging
from src.data.money import format_money_amount, parse_money_amount, quantize_money_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234.56", Decimal("1234.56")),
        ("1234,56", Decimal("1234.56")),
        ("1 234,56", Decimal("1234.56")),
        ("1\u00a0234,56", Decimal("1234.56")),
        (-10.25, Decimal("-10.25")),
        (Decimal("0.01"), Decimal("0.01")),
    ],
)
def test_parse_money_amount_accepts_supported_separators(raw, expected):
    assert parse_money_amount(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", True, "NaN", "Infinity", "1,234.56", "1.234,56", "1 23,45", "1.2.3"],
)
def test_parse_money_amount_rejects_invalid_or_ambiguous_values(raw):
    with pytest.raises(ValueError):
        parse_money_amount(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.675", Decimal("2.68")),
        ("1.005", Decimal("1.01")),
        ("-1.005", Decimal("-1.01")),
        ("0.004", Decimal("0.00")),
        ("99999999999999.99", Decimal("99999999999999.99")),
    ],
)
def test_money_amount_uses_two_decimal_half_up_rounding(raw, expected):
    assert quantize_money_amount(raw) == expected


def test_money_storage_format_is_canonical_and_does_not_use_float():
    assert format_money_amount("2.675", decimal_separator=",") == "2,68"
    assert format_money_amount("1.005", decimal_separator=",") == "1,01"
    assert format_money_amount("99999999999999.99", decimal_separator=",") == "99999999999999,99"
    assert format_money_amount("-0.004", decimal_separator=",") == "0"


@pytest.fixture
def staging_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path / "staging" / "transaction_drafts.csv", tmp_path / "transactions_info"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("2.675", "2,68"), ("1.005", "1,01"), ("1 234,565", "1234,57")],
)
def test_staging_export_applies_money_contract(staging_paths, raw, stored):
    drafts, transactions = staging_paths
    staging.append_transaction_draft(
        "2026-01-01",
        "Прочее",
        "RUB",
        raw,
        source_id="money-contract",
        path=drafts,
    )

    result = staging.export_monthly_transaction_drafts(
        "2026", "01", path=drafts, transactions_root=transactions
    )

    month = pd.read_csv(result["target_path"], sep=";", dtype=str, encoding="utf-8-sig")
    assert month.loc[0, "Прочее"] == f"{stored}|RUB|"


def test_only_edited_preview_cell_is_canonicalized(staging_paths):
    drafts, transactions = staging_paths
    target = staging.monthly_transaction_csv_path("2026", "01", transactions)
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        [{"Дата": "01.01.2026", "Прочее": "2.675|RUB|legacy", "Доход": "0"}]
    ).to_csv(target, sep=";", index=False, encoding="utf-8-sig")
    staging.append_transaction_draft(
        "2026-01-02",
        "Доход",
        "RUB",
        1,
        source_id="preview-edit",
        path=drafts,
    )
    preview, state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    preview.loc[preview["Дата"].eq("02.01.2026"), "Доход"] = "1 234,565|RUB|edited"

    result = staging.export_monthly_transaction_drafts(
        "2026",
        "01",
        path=drafts,
        transactions_root=transactions,
        preview_rows=preview.to_dict("records"),
        preview_state=state,
    )

    month = pd.read_csv(result["target_path"], sep=";", dtype=str, encoding="utf-8-sig")
    assert month.loc[month["Дата"].eq("01.01.2026"), "Прочее"].iloc[0] == "2.675|RUB|legacy"
    assert month.loc[month["Дата"].eq("02.01.2026"), "Доход"].iloc[0] == "1234,57|RUB|edited"
