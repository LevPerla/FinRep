import base64
from unittest.mock import Mock

import pandas as pd
import pytest

from src import config
from src.data.importers import bank_pdf


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
        "state": [
            {**item, "value": "too-large.pdf"}
            for item in callback["state"]
        ],
        "changedPropIds": ["kaspi-upload.contents"],
    }

    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    response = client.post("/_dash-update-component", json=payload)

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["kaspi-import-grid"]["rowData"] == []
    assert result["kaspi-import-message"]["color"] == "danger"
    assert "максимум 8 байт" in result["kaspi-import-message"]["children"]
    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_file())
    assert after == before
