import os
from datetime import datetime
from pathlib import Path
import subprocess
import sys

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard.app import create_app
from src.data.assets_editor import read_asset_snapshot
from src.data.get import clear_data_cache, get_transactions


def _write_transaction_fixture(root: Path, amount: int) -> None:
    folder = root / "transactions_info" / "2026"
    folder.mkdir(parents=True)
    pd.DataFrame({"Дата": ["01.01.2026"], "Доход": [f"{amount}|RUB|fixture"]}).to_csv(
        folder / "2026_01_.csv", sep=";", index=False
    )


def _layout_component(node, component_id: str):
    if isinstance(node, dict):
        if node.get("props", {}).get("id") == component_id:
            return node
        for value in node.values():
            found = _layout_component(value, component_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _layout_component(value, component_id)
            if found is not None:
                return found
    elif getattr(node, "id", None) == component_id:
        return node
    elif hasattr(node, "children"):
        return _layout_component(node.children, component_id)
    return None


def test_authentication_protects_dash_but_not_healthcheck():
    app = create_app()
    client = app.server.test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert client.get("/_dash-layout").status_code == 401
    assert client.get("/healthz").status_code == 200
    login_html = client.get("/login").data
    assert b'class="live-submit"' in login_html
    assert b'id="toggle-password"' in login_html
    assert b'id="password-help-button"' in login_html
    assert b'id="password-help-modal"' in login_html
    assert b"FINREP_DASH_PASSWORD" in login_html
    assert b"FINREP_DASH_SECRET_KEY" in login_html


def test_login_creates_permanent_mode_session_and_logout_clears_it():
    app = create_app()
    client = app.server.test_client()

    assert client.post("/login", data={"password": "wrong", "data_mode": "live"}).status_code == 401
    response = client.post("/login", data={"password": "test-password", "data_mode": "live"})
    assert response.status_code == 302
    assert "Expires=" in response.headers["Set-Cookie"]
    with client.session_transaction() as session:
        assert session["authenticated"] is True
        assert session["data_mode"] == "live"
        assert session.permanent is True
    layout_response = client.get("/_dash-layout")
    assert layout_response.status_code == 200
    assert b"LIVE" in layout_response.data
    assert _layout_component(layout_response.get_json(), "dashboard-month")["props"]["value"] == datetime.now().strftime("%m")

    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


def test_demo_login_works_without_configured_secrets(tmp_path):
    env = os.environ.copy()
    env.pop("FINREP_DASH_PASSWORD", None)
    env.pop("FINREP_DASH_SECRET_KEY", None)
    project_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", "from src.dashboard.app import app; client=app.server.test_client(); response=client.post('/login', data={'data_mode':'test'}); print(response.status_code, client.get('/_dash-layout').status_code)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "302 200" in result.stdout


def test_test_mode_never_requires_password():
    app = create_app()
    client = app.server.test_client()

    response = client.post("/login", data={"data_mode": "test"})
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["authenticated"] is True
        assert session["data_mode"] == "test"
    layout_response = client.get("/_dash-layout")
    assert b"TEST MODE" in layout_response.data
    assert _layout_component(layout_response.get_json(), "dashboard-year")["props"]["value"] == "2026"
    assert _layout_component(layout_response.get_json(), "dashboard-month")["props"]["value"] == "05"
    assert _layout_component(layout_response.get_json(), "dashboard-mode-badge")["props"]["children"] == "TEST MODE"
    assert _layout_component(layout_response.get_json(), "dashboard-logout-form")["props"]["action"] == "/logout"
    desktop_tabs = _layout_component(layout_response.get_json(), "dashboard-tabs")["props"]["children"]
    desktop_labels = {tab["props"]["tab_id"]: tab["props"]["label"] for tab in desktop_tabs}
    assert desktop_labels["debts"] == "Долги · Beta"
    assert desktop_labels["investments"] == "Инвестиции · Beta"


def test_month_summary_is_split_into_logical_groups():
    from src.dashboard.app import _month_summary_section
    from src.dashboard.main_data import DashboardDataset

    metrics = pd.DataFrame(
        [
            {"Показатель": "Доход", "Значение": "100₽", "Статус": "ok", "Детали": "месяц"},
            {"Показатель": "Дебиторская задолженность", "Значение": "20₽", "Статус": "watch", "Детали": "месяц"},
            {"Показатель": "Капитал", "Значение": "500₽", "Статус": "ok", "Детали": "месяц"},
        ]
    )
    dataset = DashboardDataset(id="month_summary", title="Суммарные показатели", dataframe=metrics, display_dataframe=metrics)

    section = _month_summary_section(dataset)

    assert _layout_component(section, "month-summary-cash-flow") is not None
    assert _layout_component(section, "month-summary-debts") is not None
    assert _layout_component(section, "month-summary-capital") is not None


def test_month_summary_metrics_have_explanations():
    from src.dashboard.month_data import _prepare_summary

    summary = _prepare_summary(
        pd.DataFrame(
            [
                {
                    "Дата": pd.Timestamp("2026-05-31"),
                    "Доход": 100,
                    "Дельта": 40,
                    "Баланс": 55,
                    "Капитал": 500,
                    "Капитал по активам": 540,
                    "Расхождение с активами": 40,
                    "Валютная переоценка": 5,
                }
            ]
        )
    ).set_index("Показатель")

    assert summary.loc["Дельта", "Детали"] == "Доход минус расход за выбранный месяц"
    assert summary.loc["Баланс", "Детали"] == "Cash-flow месяца с учетом сбережений и долговых операций"
    assert summary.loc["Капитал", "Детали"] == "Накопленный cash-flow на конец выбранного месяца"
    assert summary.loc["Расхождение с активами", "Детали"] == "Assets snapshot минус накопленный cash-flow капитал"


def test_planning_goals_grid_does_not_force_empty_row_space():
    css = (Path(__file__).resolve().parents[1] / "assets" / "dashboard.css").read_text(encoding="utf-8")

    assert "#planning_goals-grid .ag-layout-auto-height .ag-center-cols-viewport" in css
    assert "#planning_goals-grid .ag-layout-auto-height .ag-center-cols-container" in css
    assert "min-height: 0 !important;" in css


def test_month_transaction_rows_are_clickable():
    from src.dashboard.app import _report_table_section
    from src.dashboard.main_data import DashboardDataset

    transactions = pd.DataFrame([{"Дата": "2026-05-02", "Пища": "1 500.00₽"}])
    dataset = DashboardDataset(
        id="month_transactions",
        title="Транзакции",
        dataframe=transactions,
        display_dataframe=transactions,
    )

    section = _report_table_section(dataset, transactions, "400px")
    row = _layout_component(section, {"type": "month-transaction-day", "date": "2026-05-02"})

    assert row is not None
    assert "finrep-report-row-clickable" in row.className


def test_day_transaction_details_include_native_amount_and_comment(monkeypatch):
    from src.dashboard import month_data

    transactions = pd.DataFrame(
        [
            {"Дата": pd.Timestamp("2026-05-02"), "Категория": "Пища", "Значение": 1500, "Валюта": "RUB", "Комментарий": "Продукты"},
            {"Дата": pd.Timestamp("2026-05-02"), "Категория": "Транспорт", "Значение": 0, "Валюта": "RUB", "Комментарий": ""},
            {"Дата": pd.Timestamp("2026-05-03"), "Категория": "Пища", "Значение": 900, "Валюта": "RUB", "Комментарий": "Другой день"},
        ]
    )
    monkeypatch.setattr(month_data, "get_transactions", lambda: transactions.copy(deep=True))

    details = month_data.get_day_transaction_details("2026-05-02", "RUB")

    assert details.to_dict("records") == [
        {
            "Категория": "Пища",
            "Исходная сумма": 1500,
            "Валюта": "RUB",
            "В валюте отчета": 1500,
            "Комментарий": "Продукты",
        }
    ]


def test_transaction_modal_ignores_zero_clicks_and_tracks_theme():
    from src.dashboard.app import _clicked_transaction_date, _transaction_modal_class

    row_id = {"type": "month-transaction-day", "date": "2026-05-02"}

    assert _clicked_transaction_date(row_id, [0, 0, 0]) is None
    assert _clicked_transaction_date(row_id, 0) is None
    assert _clicked_transaction_date(row_id, [0, 1, 0]) == "2026-05-02"
    assert _transaction_modal_class("dark") == "finrep-transaction-modal finrep-modal-dark"
    assert _transaction_modal_class("light") == "finrep-transaction-modal finrep-modal-light"


def test_password_without_session_secret_fails_closed(tmp_path):
    env = os.environ.copy()
    env["FINREP_DASH_PASSWORD"] = "configured-password"
    env.pop("FINREP_DASH_SECRET_KEY", None)
    project_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(project_root)
    result = subprocess.run(
        [sys.executable, "-c", "import src.dashboard.app"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "FINREP_DASH_SECRET_KEY is required" in result.stderr


def test_live_and_test_sessions_use_separate_data_and_cache(tmp_path, monkeypatch):
    live_root = tmp_path / "live"
    sample_root = tmp_path / "sample"
    _write_transaction_fixture(live_root, 111)
    _write_transaction_fixture(sample_root, 222)
    monkeypatch.setattr(config, "DATA_PATH", str(live_root))
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(sample_root))
    clear_data_cache()

    app = create_app()
    with app.server.test_request_context("/"):
        from flask import session

        session["authenticated"] = True
        session["data_mode"] = "live"
        live_amount = get_transactions()["Значение"].iloc[0]
    with app.server.test_request_context("/"):
        from flask import session

        session["authenticated"] = True
        session["data_mode"] = "test"
        test_amount = get_transactions()["Значение"].iloc[0]

    assert live_amount == 111
    assert test_amount == 222


def test_test_mode_is_read_only_and_missing_asset_snapshot_is_not_created(tmp_path, monkeypatch):
    sample_root = tmp_path / "sample"
    assets_root = sample_root / "assets_info"
    assets_root.mkdir(parents=True)
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(sample_root))
    app = create_app()

    with app.server.test_request_context("/"):
        from flask import session

        session["authenticated"] = True
        session["data_mode"] = "test"
        assert config.active_data_path() == sample_root
        try:
            config.require_writable_mode()
        except PermissionError:
            pass
        else:
            raise AssertionError("test mode must reject writes")

        data = read_asset_snapshot("2026", "07")
        assert data.empty
        assert not (assets_root / "2026" / "2026_07.csv").exists()


def test_live_mode_still_writes_to_configured_data_root(tmp_path, monkeypatch):
    from src.data.staging import DRAFT_COLUMNS, read_transaction_drafts, write_transaction_drafts

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    app = create_app()
    row = {column: "" for column in DRAFT_COLUMNS}
    row.update({"date": "2026-07-19", "category": "Доход", "currency": "RUB", "amount": "100", "source": "test", "source_id": "test-1", "status": "draft"})

    with app.server.test_request_context("/"):
        from flask import session

        session["authenticated"] = True
        session["data_mode"] = "live"
        write_transaction_drafts(pd.DataFrame([row]))
        saved = read_transaction_drafts()

    assert saved.iloc[0]["source_id"] == "test-1"
    assert (tmp_path / "staging" / "transaction_drafts.csv").exists()


def test_sample_dashboard_reads_do_not_modify_files():
    from src.dashboard.investment_data import build_investment_dashboard_data
    from src.dashboard.main_data import build_main_dashboard_data
    from src.dashboard.month_data import build_month_dashboard_data
    from src.dashboard.planning_data import build_planning_dashboard_data
    from src.dashboard.year_data import build_year_dashboard_data
    from src.data.debts import read_debt_payments, read_debts
    from src.data.staging import read_transaction_drafts

    sample_root = Path(config.SAMPLE_DATA_PATH)
    before = {path.relative_to(sample_root): path.read_bytes() for path in sample_root.rglob("*") if path.is_file()}
    app = create_app()
    with app.server.test_request_context("/"):
        from flask import session

        session["authenticated"] = True
        session["data_mode"] = "test"
        build_main_dashboard_data("RUB", fx_network_enabled=False, year="2026", month="05")
        build_year_dashboard_data("2026", "RUB", fx_network_enabled=False)
        build_month_dashboard_data("2026", "05", "RUB", fx_network_enabled=False)
        build_planning_dashboard_data("2026", "RUB", fx_network_enabled=False)
        build_investment_dashboard_data("RUB", fx_network_enabled=False)
        read_debts()
        read_debt_payments()
        read_transaction_drafts()
        read_asset_snapshot("2026", "07")

    after = {path.relative_to(sample_root): path.read_bytes() for path in sample_root.rglob("*") if path.is_file()}
    assert after == before
