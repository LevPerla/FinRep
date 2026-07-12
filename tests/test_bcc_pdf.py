from src.data.importers.bcc_pdf import BCC_SOURCE, parse_bcc_pdf


def test_parse_bcc_statement_includes_pending_transactions():
    data = parse_bcc_pdf("data/bank_data/bcc/Document.pdf")

    assert len(data) == 15
    assert set(data["source"]) == {BCC_SOURCE}
    assert set(data["currency"]) == {"KZT"}

    pending = data.loc[
        data["date"].eq("2026-07-12") & data["details"].eq("Pending Trip.com") & data["amount"].eq(109860.71)
    ].iloc[0]
    assert pending["category"] != "Доход"

    tickets = data.loc[data["details"].eq("Purchase TICKETS KZ")].iloc[0]
    assert tickets["date"] == "2026-06-21"
    assert tickets["amount"] == 300406.0
    assert tickets["category"] != "Доход"

    top_up = data.loc[(data["date"].eq("2026-06-21")) & data["details"].eq("Top-up")].iloc[0]
    assert top_up["amount"] == 199999.5
    assert top_up["category"] == "Доход"
