import pytest

from src.dashboard import investment_data, main_data, month_data, planning_data, year_data
from src.data import get_finance


BUILDERS = [
    (main_data, "_build_main_dashboard_data", main_data.build_main_dashboard_data, ("RUB",), {"year": "2026", "month": "01"}),
    (year_data, "_build_year_dashboard_data", year_data.build_year_dashboard_data, ("2026", "RUB"), {}),
    (month_data, "_build_month_dashboard_data", month_data.build_month_dashboard_data, ("2026", "01", "RUB"), {}),
    (planning_data, "_build_planning_dashboard_data", planning_data.build_planning_dashboard_data, ("2026", "RUB"), {}),
    (investment_data, "_build_investment_dashboard_data", investment_data.build_investment_dashboard_data, ("RUB",), {}),
]


@pytest.mark.parametrize("module,core_name,builder,args,kwargs", BUILDERS)
def test_dashboard_builder_uses_requested_network_mode_and_restores_previous(
    monkeypatch, module, core_name, builder, args, kwargs
):
    observed = []
    monkeypatch.setattr(
        module,
        core_name,
        lambda *_args, **_kwargs: observed.append(get_finance._FX_NETWORK_ENABLED.get()) or {},
    )
    get_finance.set_fx_network_enabled(True)

    builder(*args, fx_network_enabled=False, **kwargs)

    assert observed == [False]
    assert get_finance._FX_NETWORK_ENABLED.get() is True
    get_finance.set_fx_network_enabled(False)


def test_builder_restores_network_mode_after_exception(monkeypatch):
    def fail(*_args, **_kwargs):
        assert get_finance._FX_NETWORK_ENABLED.get() is True
        raise RuntimeError("synthetic builder failure")

    monkeypatch.setattr(investment_data, "_build_investment_dashboard_data", fail)
    get_finance.set_fx_network_enabled(False)

    with pytest.raises(RuntimeError, match="synthetic builder failure"):
        investment_data.build_investment_dashboard_data("RUB", fx_network_enabled=True)

    assert get_finance._FX_NETWORK_ENABLED.get() is False


def test_online_builder_does_not_leak_into_following_offline_builder(monkeypatch):
    observed = []
    monkeypatch.setattr(
        investment_data,
        "_build_investment_dashboard_data",
        lambda *_: observed.append(get_finance._FX_NETWORK_ENABLED.get()) or {},
    )
    get_finance.set_fx_network_enabled(False)

    investment_data.build_investment_dashboard_data("RUB", fx_network_enabled=True)
    investment_data.build_investment_dashboard_data("RUB", fx_network_enabled=False)

    assert observed == [True, False]
    assert get_finance._FX_NETWORK_ENABLED.get() is False
