import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, Mock

import pytest

from src.dashboard import export


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
    return None


def _component_text(node) -> str:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return _component_text(node.get("props", {}).get("children"))
    if isinstance(node, list):
        return "".join(_component_text(value) for value in node)
    return ""


def _export_callback_request(app, client, locale="ru"):
    key = next(key for key in app.callback_map if "page-export-download.data" in key)
    callback = app.callback_map[key]
    outputs = callback["output"]
    if isinstance(outputs, list):
        serialized_outputs = [
            {"id": item.component_id, "property": item.component_property}
            for item in outputs
        ]
    else:
        serialized_outputs = {
            "id": outputs.component_id,
            "property": outputs.component_property,
        }
    payload = {
        "output": key,
        "outputs": serialized_outputs,
        "inputs": [
            {"id": "export-png", "property": "n_clicks", "value": 1},
            {"id": "export-pdf", "property": "n_clicks", "value": 0},
        ],
        "state": [
            {"id": "dashboard-currency", "property": "value", "value": "RUB"},
            {"id": "dashboard-year", "property": "value", "value": "2026"},
            {"id": "dashboard-month", "property": "value", "value": "05"},
            {"id": "dashboard-tabs", "property": "active_tab", "value": "main"},
            {"id": "main-report-tabs", "property": "active_tab", "value": "overview"},
            {"id": "dashboard-locale", "property": "data", "value": locale},
        ],
        "changedPropIds": ["export-png.n_clicks"],
    }
    return client.post("/_dash-update-component", json=payload)


def test_test_mode_disables_heavy_export_and_server_rejects_direct_callback(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard import app as app_module

    app = app_module.create_app()
    client = app.server.test_client()
    client.post("/login", data={"data_mode": "test"})
    layout = client.get("/_dash-layout").get_json()

    assert _layout_component(layout, "export-png")["props"]["disabled"] is True
    assert _layout_component(layout, "export-pdf")["props"]["disabled"] is True
    message = _layout_component(layout, "page-export-message")
    assert message["props"]["is_open"] is True
    assert "только в LIVE" in _component_text(message)

    render = Mock(side_effect=AssertionError("TEST must not start Chromium export"))
    monkeypatch.setattr(app_module, "export_dashboard_page", render)
    response = _export_callback_request(app, client)

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert "page-export-download" not in result
    assert result["page-export-message"]["color"] == "warning"
    assert "только в LIVE" in result["page-export-message"]["children"]
    render.assert_not_called()


def test_live_mode_keeps_export_enabled_and_message_closed(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    client.post("/login", data={"password": "synthetic-password", "data_mode": "live"})
    layout = client.get("/_dash-layout").get_json()

    assert _layout_component(layout, "export-png")["props"].get("disabled") is not True
    assert _layout_component(layout, "export-pdf")["props"].get("disabled") is not True
    assert _layout_component(layout, "page-export-message")["props"]["is_open"] is False


def test_live_callback_returns_busy_message_without_download(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard import app as app_module

    app = app_module.create_app()
    client = app.server.test_client()
    client.post(
        "/login",
        data={"password": "synthetic-password", "data_mode": "live"},
    )
    monkeypatch.setattr(
        app_module,
        "export_dashboard_page",
        Mock(side_effect=export.ExportBusyError("Экспорт уже выполняется. Повторите после завершения.")),
    )

    response = _export_callback_request(app, client)

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert "page-export-download" not in result
    assert result["page-export-message"]["color"] == "warning"
    assert "Экспорт уже выполняется" in result["page-export-message"]["children"]


def test_live_callback_passes_english_locale_and_localizes_feedback(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard import app as app_module

    app = app_module.create_app()
    client = app.server.test_client()
    client.post(
        "/login",
        data={"password": "synthetic-password", "data_mode": "live"},
    )
    output = tmp_path / "export.png"
    output.write_bytes(b"png")
    render = Mock(return_value=output)
    monkeypatch.setattr(app_module, "export_dashboard_page", render)

    response = _export_callback_request(app, client, locale="en")

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["page-export-message"]["children"] == "Export ready."
    assert render.call_args.kwargs["locale"] == "en"


class _Page:
    def goto(self, *args, **kwargs):
        pass

    def screenshot(self, **kwargs):
        pass


class _Context:
    def add_init_script(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        pass

    def route_web_socket(self, *args, **kwargs):
        pass

    def new_page(self):
        return _Page()


class _Browser:
    def new_context(self, **kwargs):
        return _Context()

    def close(self):
        pass


class _Playwright:
    @property
    def chromium(self):
        return self

    def launch(self):
        return _Browser()


class _BlockingPlaywright:
    def __init__(self, entered: Event, finish: Event):
        self.entered = entered
        self.finish = finish

    def __enter__(self):
        self.entered.set()
        assert self.finish.wait(timeout=5)
        return _Playwright()

    def __exit__(self, *args):
        pass


def test_second_concurrent_export_fails_fast_and_next_export_recovers(tmp_path, monkeypatch):
    entered = Event()
    finish = Event()
    monkeypatch.setattr(export.config, "REPORTS_PATH", str(tmp_path))
    monkeypatch.setattr(export, "_wait_for_dashboard_ready", lambda page: None)
    monkeypatch.setattr(export, "sync_playwright", lambda: _BlockingPlaywright(entered, finish))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(export.export_dashboard_page, "RUB", "main", "png")
        assert entered.wait(timeout=5)
        started = time.monotonic()
        with pytest.raises(export.ExportBusyError, match="уже выполняется"):
            export.export_dashboard_page("RUB", "main", "png")
        assert time.monotonic() - started < 0.5
        finish.set()
        assert first.result(timeout=5).suffix == ".png"

    assert export.export_dashboard_page("RUB", "main", "png").suffix == ".png"


def test_export_lock_is_released_after_playwright_error(tmp_path, monkeypatch):
    monkeypatch.setattr(export.config, "REPORTS_PATH", str(tmp_path))
    monkeypatch.setattr(
        export,
        "sync_playwright",
        Mock(side_effect=export.PlaywrightError("synthetic failure")),
    )

    with pytest.raises(RuntimeError, match="Chromium is not available"):
        export.export_dashboard_page("RUB", "main", "png")

    assert export._EXPORT_LOCK.acquire(blocking=False)
    export._EXPORT_LOCK.release()


def test_export_browser_receives_selected_locale_before_page_load(tmp_path, monkeypatch):
    page = MagicMock()
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context
    playwright = MagicMock()
    playwright.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = playwright
    monkeypatch.setattr(export.config, "REPORTS_PATH", str(tmp_path))
    monkeypatch.setattr(export, "sync_playwright", lambda: manager)
    monkeypatch.setattr(export, "_wait_for_dashboard_ready", lambda page: None)

    export.export_dashboard_page("RUB", "main", "png", locale="en")

    script = context.add_init_script.call_args.args[0]
    assert "dashboard-locale" in script
    assert '\\"en\\"' in script
    page.goto.assert_called_once()


def test_docker_runs_one_process_for_process_local_export_lock():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")

    assert "gunicorn --workers 1 --worker-class gthread --threads 2" in dockerfile
