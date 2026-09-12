import json
from decimal import Decimal

import pandas as pd
import pytest

from src import config
from src.data.assets_editor import read_asset_snapshot, write_asset_snapshot


@pytest.fixture
def assets_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path / "assets_info"


def _put_snapshot(root, cell):
    path = root / "2026" / "2026_01.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"Счет": "Synthetic", "Сумма": cell}]).to_csv(
        path, sep=";", index=False, encoding="utf-8-sig"
    )
    return path


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("2.675", "2,68|RUB"),
        ("1.005", "1,01|USD"),
        ("1 234,565", "1234,57|EUR"),
        ("1\u00a0234,565", "1234,57|KZT"),
        ("99999999999999.99", "99999999999999,99|GBP"),
    ],
)
def test_explicit_asset_write_uses_money_contract(assets_root, raw, stored):
    currency = stored.split("|")[1]

    result = write_asset_snapshot(
        [{"account": "Synthetic", "amount": raw, "currency": currency}],
        "2026",
        "01",
    )

    saved = pd.read_csv(result["path"], sep=";", dtype=str, encoding="utf-8-sig")
    assert saved.loc[0, "Сумма"] == stored


def test_asset_read_keeps_exact_decimal_without_rewriting_file(assets_root):
    path = _put_snapshot(assets_root, "99999999999999.99|RUB")
    before = path.read_bytes()

    row = read_asset_snapshot("2026", "01").iloc[0]

    assert row["amount"] == Decimal("99999999999999.99")
    assert path.read_bytes() == before
    assert not (assets_root.parent / "backups").exists()


@pytest.mark.parametrize("raw", ["1,234.56", "1.234,56", "1 23,45"])
def test_ambiguous_asset_amount_does_not_replace_existing_snapshot(assets_root, raw):
    path = _put_snapshot(assets_root, "10|RUB")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="Некорректная сумма"):
        write_asset_snapshot(
            [{"account": "Synthetic", "amount": raw, "currency": "RUB"}],
            "2026",
            "01",
        )

    assert path.read_bytes() == before
    assert not (assets_root.parent / "backups").exists()


def test_asset_grid_record_preserves_large_amount_for_editing(assets_root, monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    _put_snapshot(assets_root, "99999999999999.99|RUB")
    from src.dashboard.app import _asset_input_records

    records = _asset_input_records("2026", "01")

    assert records[0]["amount"] == "99 999 999 999 999.99"
    assert records[0]["amount_sort"] == 1
    json.dumps(records)
