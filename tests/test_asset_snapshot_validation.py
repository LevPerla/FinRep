from pathlib import Path

import pandas as pd
import pytest
from flask import Flask, session

from src import config
from src.data.assets_editor import read_asset_snapshot, write_asset_snapshot


@pytest.fixture
def assets_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path / "assets_info"


def put_snapshot(root, cell, month="01"):
    path = root / "2026" / f"2026_{month}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{"Счет":"Synthetic account", "Сумма":cell}]).to_csv(path, sep=";", index=False)
    return path


def files(root):
    return {str(path.relative_to(root)):path.read_bytes() for path in root.rglob("*.csv")}


@pytest.mark.parametrize("cell", ["broken|RUB", "10|ZZZ", "broken|ZZZ", "NaN|RUB", "NaN", "inf|USD", "-inf|RUB", "1e10000|RUB", "10|RUB|extra"])
def test_invalid_existing_snapshot_reports_row_and_preserves_file(assets_root, cell):
    put_snapshot(assets_root, cell)
    before = files(assets_root)
    with pytest.raises(ValueError) as error:
        read_asset_snapshot("2026", "01")
    message = str(error.value)
    assert "2026_01.csv" in message
    assert "строка 2" in message
    assert "Synthetic account" in message
    assert str(assets_root) not in message
    assert files(assets_root) == before


@pytest.mark.parametrize("mode", ["live", "test"])
def test_invalid_previous_snapshot_is_not_copied_into_next_month(assets_root, monkeypatch, mode):
    put_snapshot(assets_root, "broken|ZZZ")
    before = files(assets_root)
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(assets_root.parent))
    app = Flask(__name__)
    app.secret_key = "synthetic-key"
    with app.test_request_context("/"):
        session["authenticated"] = True
        session["data_mode"] = mode
        with pytest.raises(ValueError, match="2026_01.csv"):
            read_asset_snapshot("2026", "02")
    assert files(assets_root) == before


@pytest.mark.parametrize("columns", [{"Счет":["Synthetic"]}, {"Сумма":["10|RUB"]}])
def test_missing_required_column_does_not_invent_account_or_zero(assets_root, columns):
    path = put_snapshot(assets_root, "10|RUB")
    pd.DataFrame(columns).to_csv(path, sep=";", index=False)
    before = files(assets_root)
    with pytest.raises(ValueError, match="2026_01.csv"):
        read_asset_snapshot("2026", "01")
    assert files(assets_root) == before


@pytest.mark.parametrize("cell,amount,currency", [
    ("100",100,"RUB"), ("100|RUB",100,"RUB"), ("-10,25|USD",-10.25,"USD"),
    ("1 234,56| eur",1234.56,"EUR"), ("1\u00a0234.56|KZT",1234.56,"KZT"),
    ("0|GBP",0,"GBP"), ("",0,"RUB"), ("|USD",0,"USD"), ("10|",10,"RUB"),
])
def test_valid_legacy_formats_are_preserved(assets_root, cell, amount, currency):
    put_snapshot(assets_root, cell)
    row = read_asset_snapshot("2026", "01").iloc[0]
    assert row["amount"] == amount
    assert row["currency"] == currency


def test_read_then_write_cannot_replace_corrupt_amount_with_zero(assets_root):
    put_snapshot(assets_root, "broken|RUB")
    before = files(assets_root)
    with pytest.raises(ValueError):
        rows = read_asset_snapshot("2026", "01").to_dict("records")
        write_asset_snapshot(rows,"2026","01")
    assert files(assets_root.parent) == {str(Path("assets_info")/key):value for key,value in before.items()}


def test_valid_previous_snapshot_keeps_existing_live_copy_behavior(assets_root):
    put_snapshot(assets_root,"12,50|USD")
    row = read_asset_snapshot("2026","02").iloc[0]
    assert row["amount"] == 12.5
    assert (assets_root/"2026"/"2026_02.csv").exists()


def test_empty_snapshot_with_headers_is_valid(assets_root):
    path = put_snapshot(assets_root,"0")
    path.write_text("Счет;Сумма\n")
    assert read_asset_snapshot("2026","01").empty


@pytest.mark.parametrize("contents", [b"", b'\xd1\xa1\xd1\x87\xd0\xb5\xd1\x82;\xd0\xa1\xd1\x83\xd0\xbc\xd0\xbc\xd0\xb0\n"unterminated', b'\xff\xff'])
def test_unreadable_csv_reports_filename_without_changing_it(assets_root, contents):
    path = put_snapshot(assets_root,"0")
    path.write_bytes(contents)
    before = files(assets_root)
    with pytest.raises(ValueError,match="2026_01.csv"):
        read_asset_snapshot("2026","01")
    assert files(assets_root) == before


def test_load_callback_displays_error_and_does_not_invent_zero(assets_root, monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD","synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY","synthetic-key")
    from src.dashboard.app import create_app
    app = create_app()
    put_snapshot(assets_root,"broken|ZZZ")
    client = app.server.test_client()
    with client.session_transaction() as context:
        context["authenticated"] = True
        context["data_mode"] = "live"
    key = next(key for key in app.callback_map if "assets-input-message.children" in key)
    callback = app.callback_map[key]
    values = {"assets-load-button":1,"assets-add-row-button":0,"assets-delete-row-button":0,"assets-apply-button":0,
              "dashboard-year":"2026","dashboard-month":"01","assets-input-grid":[]}
    payload = {"output":key,"outputs":[{"id":item.component_id,"property":item.component_property} for item in callback["output"]],
               "inputs":[{**item,"value":values.get(item["id"])} for item in callback["inputs"]],
               "state":[{**item,"value":values.get(item["id"])} for item in callback["state"]],
               "changedPropIds":["assets-load-button.n_clicks"]}
    before = files(assets_root)
    response = client.post("/_dash-update-component",json=payload)
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["assets-input-message"]["color"] == "danger"
    assert "строка 2" in result["assets-input-message"]["children"]
    assert "Synthetic account" in result["assets-input-message"]["children"]
    assert result["assets-input-grid"]["rowData"] == []
    assert files(assets_root) == before
