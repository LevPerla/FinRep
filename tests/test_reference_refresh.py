from contextlib import nullcontext
from datetime import datetime, timezone

from src.dashboard import app as app_module


def test_reference_refresh_due_uses_latest_successful_fetch():
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)

    assert app_module._reference_refresh_due([], now) is True
    assert app_module._reference_refresh_due(
        [{"fetched_at": "2026-09-29T23:59:59Z"}], now) is True
    assert app_module._reference_refresh_due(
        [{"fetched_at": "2026-09-01T00:00:00Z"},
         {"fetched_at": "2026-10-01T00:00:01Z"}], now) is False


def test_refresh_stale_reference_data_refreshes_fx_and_cpi_independently(monkeypatch):
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)
    calls = []
    monkeypatch.setattr(app_module.config, "is_test_mode", lambda: False)
    monkeypatch.setattr(app_module.config, "use_sqlite_storage", lambda: True)
    monkeypatch.setattr(app_module.config, "active_database_path", lambda: "db")
    fx_rows = iter([
        [{"fetched_at": "2026-09-01T00:00:00Z"}],
        [{"fetched_at": "2026-10-07T00:00:00Z"}],
    ])
    monkeypatch.setattr(app_module, "fx_rates", lambda _database: next(fx_rows))
    monkeypatch.setattr(app_module, "cpi_observations", lambda _database: [
        {"fetched_at": "2026-10-06T00:00:00Z"}])
    monkeypatch.setattr(app_module, "fx_network_mode", lambda enabled: nullcontext())
    monkeypatch.setattr(
        app_module, "get_usd_rates",
        lambda currencies, start, end: calls.append(("fx", tuple(currencies), start, end)))
    monkeypatch.setattr(
        app_module, "refresh_official_cpi",
        lambda database: calls.append(("cpi", database)) or {"status": "done"})

    fx_result, cpi_result = app_module._refresh_stale_reference_data(now)

    assert [call[0] for call in calls] == ["fx"]
    assert fx_result == {"request": "auto", "status": "done"}
    assert cpi_result is app_module.no_update

    calls.clear()
    monkeypatch.setattr(app_module, "fx_rates", lambda _database: [
        {"fetched_at": "2026-10-06T00:00:00Z"}])
    monkeypatch.setattr(app_module, "cpi_observations", lambda _database: [
        {"fetched_at": "2026-09-01T00:00:00Z"}])

    fx_result, cpi_result = app_module._refresh_stale_reference_data(now)

    assert calls == [("cpi", "db")]
    assert fx_result is app_module.no_update
    assert cpi_result == {"status": "done"}


def test_refresh_stale_reference_data_never_uses_network_in_test_mode(monkeypatch):
    monkeypatch.setattr(app_module.config, "is_test_mode", lambda: True)
    monkeypatch.setattr(
        app_module, "fx_rates", lambda _database: (_ for _ in ()).throw(AssertionError))

    assert app_module._refresh_stale_reference_data() == (
        app_module.no_update, app_module.no_update)
