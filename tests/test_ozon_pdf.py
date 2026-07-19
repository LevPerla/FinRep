from src.data.importers.ozon_pdf import OZON_SOURCE, parse_ozon_pdf


def test_parse_ozon_statement():
    data = parse_ozon_pdf("data/bank_data/ozon/выписка.pdf")

    assert len(data) == 4
    assert set(data["source"]) == {OZON_SOURCE}
    assert set(data["currency"]) == {"RUB"}
    assert data["amount"].sum() == 15359.0
    assert not data["category"].eq("Доход").any()

    ozon_order = data.loc[data["details"].str.contains("19585537-0126", regex=False)].iloc[0]
    assert ozon_order["date"] == "2026-06-24"
    assert ozon_order["amount"] == 384.0
