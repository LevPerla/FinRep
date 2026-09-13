import importlib
import sys

from src.data import validation
from src.reports import main_report, month_report, year_report


def test_import_is_inert_and_main_generates_all_reports(monkeypatch):
    calls = []

    monkeypatch.setattr(
        validation,
        "validate_all_data",
        lambda: calls.append(("validate", {})),
    )
    monkeypatch.setattr(
        main_report,
        "create_main_report",
        lambda **kwargs: calls.append(("main", kwargs)),
    )
    monkeypatch.setattr(
        year_report,
        "create_year_report",
        lambda **kwargs: calls.append(("year", kwargs)),
    )
    monkeypatch.setattr(
        month_report,
        "create_month_report",
        lambda **kwargs: calls.append(("month", kwargs)),
    )
    monkeypatch.delitem(sys.modules, "main", raising=False)

    entrypoint = importlib.import_module("main")

    assert calls == []

    entrypoint.main()

    assert calls == [
        ("validate", {}),
        ("main", {"currency": "RUB", "fx_network_enabled": True, "locale": "ru"}),
        (
            "year",
            {"year": "2026", "currency": "RUB", "fx_network_enabled": True, "locale": "ru"},
        ),
        (
            "month",
            {
                "year": "2026",
                "currency": "RUB",
                "month": "04",
                "fx_network_enabled": True,
                "locale": "ru",
            },
        ),
    ]
