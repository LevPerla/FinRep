from pathlib import Path

from src import config
from src.data.importers.ozon_pdf import OZON_SOURCE, parse_ozon_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "bank_statements" / "ozon_synthetic.pdf"


def test_parse_ozon_statement(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    data = parse_ozon_pdf(FIXTURE)

    assert len(data) == 4
    assert set(data["source"]) == {OZON_SOURCE}
    assert set(data["currency"]) == {"RUB"}
    assert data["amount"].sum() == 15359.0
    assert not data["category"].eq("Доход").any()

    ozon_order = data.loc[data["details"].str.contains("19585537-0126", regex=False)].iloc[0]
    assert ozon_order["date"] == "2026-06-24"
    assert ozon_order["amount"] == 384.0
