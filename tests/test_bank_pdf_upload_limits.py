import base64
from unittest.mock import Mock

import pandas as pd
import pytest

from src import config
from src.data.importers import bank_pdf
from src.data.sqlite_store import initialize_database


def _contents(payload: bytes) -> str:
    return "data:application/pdf;base64," + base64.b64encode(payload).decode("ascii")


def _opened_pdf(pages):
    opened = Mock()
    opened.__enter__ = Mock(return_value=Mock(pages=pages))
    opened.__exit__ = Mock(return_value=False)
    return opened


def test_upload_over_size_limit_stops_before_decode_or_pdf_open(monkeypatch):
    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_BYTES", 8)
    contents = _contents(b"123456789")
    decode = Mock(side_effect=AssertionError("oversized payload must not be decoded"))
    opened = Mock(side_effect=AssertionError("oversized payload must not reach pdfplumber"))
    monkeypatch.setattr(bank_pdf.base64, "b64decode", decode)
    monkeypatch.setattr(bank_pdf.pdfplumber, "open", opened)

    with pytest.raises(bank_pdf.BankPdfLimitError, match="максимум 8 байт"):
        bank_pdf.parse_bank_upload_contents(contents)

    decode.assert_not_called()
    opened.assert_not_called()


def test_upload_at_exact_size_limit_reaches_selected_parser(monkeypatch):
    payload = b"12345678"
    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_BYTES", len(payload))
    first_page = Mock()
    first_page.extract_text.return_value = bank_pdf.OZON_MARKER
    monkeypatch.setattr(bank_pdf.pdfplumber, "open", Mock(return_value=_opened_pdf([first_page])))
    expected = pd.DataFrame([{"amount": 1.0}])
    parser = Mock(return_value=expected)
    monkeypatch.setattr(bank_pdf, "parse_ozon_pdf_bytes", parser)

    actual = bank_pdf.parse_bank_upload_contents(_contents(payload))

    pd.testing.assert_frame_equal(actual, expected)
    parser.assert_called_once_with(payload)


def test_batch_limits_count_and_total_size(monkeypatch):
    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_BATCH_FILES", 2)
    with pytest.raises(bank_pdf.BankPdfLimitError, match="не больше 2"):
        bank_pdf.validate_bank_upload_batch([
            _contents(b"1"), _contents(b"2"), _contents(b"3"),
        ])

    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_BATCH_BYTES", 4)
    with pytest.raises(bank_pdf.BankPdfLimitError, match="Общий размер"):
        bank_pdf.validate_bank_upload_batch([_contents(b"123"), _contents(b"45")])


def test_upload_over_page_limit_stops_before_text_extraction_or_parser(monkeypatch):
    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_PAGES", 2)
    pages = [Mock(), Mock(), Mock()]
    monkeypatch.setattr(bank_pdf.pdfplumber, "open", Mock(return_value=_opened_pdf(pages)))
    parser = Mock(side_effect=AssertionError("oversized PDF must not reach a bank parser"))
    monkeypatch.setattr(bank_pdf, "parse_kaspi_pdf_bytes", parser)

    with pytest.raises(bank_pdf.BankPdfLimitError, match="3 стр.*максимум 2"):
        bank_pdf.parse_bank_upload_contents(_contents(b"synthetic"))

    pages[0].extract_text.assert_not_called()
    parser.assert_not_called()


def test_dashboard_reports_upload_limits_and_sets_transport_backstop(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import _transaction_input_layout, create_app

    app = create_app()
    layout = _transaction_input_layout("RUB", "2026", "01", "light")

    assert app.server.config["MAX_CONTENT_LENGTH"] == bank_pdf.MAX_BANK_PDF_REQUEST_BYTES
    assert "до 10 MiB и 50 страниц" in str(layout)
    assert "Остальные остатки не сохраняются автоматически" in str(layout)
    assert "data-max-total-bytes" in str(layout)


def test_transport_backstop_rejects_request_body_before_dash_callback(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    app.server.config["MAX_CONTENT_LENGTH"] = 64
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    response = client.post(
        "/_dash-update-component",
        data=b"x" * 65,
        content_type="application/json",
    )

    assert response.status_code == 413


def test_upload_limit_error_clears_preview_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(bank_pdf, "MAX_BANK_PDF_BYTES", 8)
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    key = next(key for key in app.callback_map if "kaspi-import-grid.rowData" in key)
    callback = app.callback_map[key]
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [
            {**item, "value": _contents(b"123456789")}
            for item in callback["inputs"]
        ],
        "state": [{
            **item,
            "value": (
                "ru" if item["id"] == "dashboard-locale" else
                [] if item["id"] == "kaspi-import-grid" else
                "too-large.pdf"
            ),
        } for item in callback["state"]],
        "changedPropIds": ["kaspi-upload.contents"],
    }

    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    response = client.post("/_dash-update-component", json=payload)

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert "kaspi-import-grid" not in result
    assert result["kaspi-import-message"]["color"] == "danger"
    assert "максимум 8 байт" in result["kaspi-import-message"]["children"]
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    assert after == before


def test_valid_unknown_pdf_is_reported_as_unsupported(monkeypatch):
    first_page = Mock()
    first_page.extract_text.return_value = "Unrecognized document"
    monkeypatch.setattr(bank_pdf.pdfplumber, "open", Mock(return_value=_opened_pdf([first_page])))
    monkeypatch.setattr(bank_pdf, "parse_kaspi_pdf_bytes", Mock(return_value=pd.DataFrame()))

    with pytest.raises(bank_pdf.BankPdfUnsupportedError, match="не похож на поддерживаемую"):
        bank_pdf.parse_bank_upload_contents(_contents(b"valid-unknown-pdf"))


def test_recognized_statement_without_transactions_is_reported_as_empty(monkeypatch):
    first_page = Mock()
    first_page.extract_text.return_value = bank_pdf.OZON_MARKER
    monkeypatch.setattr(bank_pdf.pdfplumber, "open", Mock(return_value=_opened_pdf([first_page])))
    monkeypatch.setattr(bank_pdf, "parse_ozon_pdf_bytes", Mock(return_value=pd.DataFrame()))

    with pytest.raises(bank_pdf.BankPdfEmptyError, match="операции в ней не найдены"):
        bank_pdf.parse_bank_upload_contents(_contents(b"valid-empty-statement"))


def test_malformed_pdf_is_reported_without_parser_details(monkeypatch):
    monkeypatch.setattr(
        bank_pdf.pdfplumber,
        "open",
        Mock(side_effect=RuntimeError("/Users/owner/private-statement.pdf: No /Root object")),
    )

    with pytest.raises(bank_pdf.BankPdfReadError) as error:
        bank_pdf.parse_bank_upload_contents(_contents(b"malformed"))

    assert str(error.value) == "Не удалось прочитать PDF. Проверь файл и попробуй снова."
    assert "/Users/owner" not in str(error.value)


def test_dashboard_hides_private_error_details_and_logs_diagnostics(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    from src.dashboard import app as dashboard_app

    private_detail = "/Users/owner/private-statement.pdf: parser failure"
    monkeypatch.setattr(
        dashboard_app,
        "parse_bank_upload_contents",
        Mock(side_effect=RuntimeError(private_detail)),
    )
    app = dashboard_app.create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    key = next(key for key in app.callback_map if "kaspi-import-grid.rowData" in key)
    callback = app.callback_map[key]
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [{**item, "value": _contents(b"malformed")} for item in callback["inputs"]],
        "state": [{
            **item,
            "value": (
                "ru" if item["id"] == "dashboard-locale" else
                [] if item["id"] == "kaspi-import-grid" else
                "/Users/owner/private-statement.pdf"
            ),
        } for item in callback["state"]],
        "changedPropIds": ["kaspi-upload.contents"],
    }

    with caplog.at_level("ERROR", logger="src.dashboard.app"):
        response = client.post("/_dash-update-component", json=payload)

    result = response.get_json()["response"]
    message = result["kaspi-import-message"]["children"]
    assert response.status_code == 200
    assert result["kaspi-import-message"]["color"] == "danger"
    assert message == "private-statement.pdf: импорт не выполнен из-за внутренней ошибки."
    assert "/Users/owner" not in message
    assert private_detail in caplog.text


def test_dashboard_batch_keeps_ready_files_and_marks_cross_file_duplicates(
    tmp_path, monkeypatch
):
    database = tmp_path / "finrep.sqlite3"
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    initialize_database(database)
    from src.dashboard import app as dashboard_app

    def parsed(source, balance):
        frame = pd.DataFrame([{
            "source": source,
            "source_id": f"{source}-1",
            "date": "2026-10-01",
            "currency": "USD",
            "amount": "10",
            "direction": "credit",
            "comment": "Interest payment",
            "category": "Прочие доходы",
            "details": "Interest payment",
            "import_action": "import",
            "skip_reason": "",
            "duplicate_in_source": False,
            "duplicate_in_staging": False,
        }])
        frame.attrs["statement_balance"] = {
            "account_id": source,
            "balance": balance,
            "currency": "USD",
            "as_of_date": "2026-10-31",
        }
        return frame

    parser = Mock(side_effect=[
        parsed("kaspi_pdf", "100"),
        bank_pdf.BankPdfLimitError("PDF содержит 51 стр.; максимум 50."),
        parsed("ozon_pdf", "200"),
    ])
    monkeypatch.setattr(dashboard_app, "parse_bank_upload_contents", parser)
    app = dashboard_app.create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    key = next(
        key for key in app.callback_map if "bank-statement-balances.data" in key
    )
    callback = app.callback_map[key]
    values = {
        "kaspi-upload": [
            _contents(b"first"), _contents(b"broken"), _contents(b"third"),
        ],
        "dashboard-locale": "ru",
        "kaspi-import-grid": [],
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [
            {**item, "value": values[item["id"]]}
            for item in callback["inputs"]
        ],
        "state": [{
            **item,
            "value": (
                ["first.pdf", "broken.pdf", "third.pdf"]
                if item["id"] == "kaspi-upload" else values[item["id"]]
            ),
        } for item in callback["state"]],
        "changedPropIds": ["kaspi-upload.contents"],
    }
    response = client.post("/_dash-update-component", json=payload)

    assert response.status_code == 200
    result = response.get_json()["response"]
    rows = result["kaspi-import-grid"]["rowData"]
    assert len(rows) == 2
    assert {row["source_file"] for row in rows} == {"first.pdf", "third.pdf"}
    assert {row["import_action"] for row in rows} == {"review"}
    assert "broken.pdf — ошибка" in result["bank-upload-status"]["children"]
    assert result["kaspi-import-message"]["color"] == "danger"
    assert "broken.pdf: PDF содержит 51 стр.; максимум 50." in result["kaspi-import-message"]["children"]
    balances = result["bank-statement-balances"]["data"]
    assert [item["source_file"] for item in balances] == ["first.pdf", "third.pdf"]
    assert result["bank-statement-balance"]["data"] == balances[0]

    source_key = next(
        key for key in app.callback_map
        if "bank-statement-balance-source-container.style" in key
    )
    source_callback = app.callback_map[source_key]
    source_response = client.post("/_dash-update-component", json={
        "output": source_key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in source_callback["output"]
        ],
        "inputs": [{**source_callback["inputs"][0], "value": balances}],
        "state": [{**source_callback["state"][0], "value": None}],
        "changedPropIds": ["bank-statement-balances.data"],
    })
    source_result = source_response.get_json()["response"]
    assert source_result["bank-statement-balance-source-container"]["style"] == {
        "display": "block"
    }
    source = source_result["bank-statement-balance-source"]
    assert source["value"] == 0
    assert [item["label"].split(" · ")[0] for item in source["options"]] == [
        "first.pdf", "third.pdf",
    ]
