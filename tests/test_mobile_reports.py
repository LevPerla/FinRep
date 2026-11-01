import pandas as pd
import plotly.graph_objects as go
import pytest

from src.dashboard.main_data import DashboardDataset, _fx_changes_figure
from src.dashboard.mobile_reports import mobile_figure, mobile_rows, currency_rows, select_mobile_series
from src.dashboard.report_tables import _grid_row_data, _report_table_style_maps, _report_cell_style


def test_mobile_line_removes_overlapping_title_and_labels_without_mutating_desktop():
    dates = pd.date_range("2023-01-01", periods=48, freq="MS")
    desktop = go.Figure(go.Scatter(x=dates, y=list(range(48)), name="Капитал", mode="lines+markers+text"))
    desktop.update_layout(title="Капитал", legend=dict(y=1.02), xaxis_rangeslider_visible=True)
    before = desktop.to_json()
    mobile = mobile_figure(desktop, "capital")
    assert desktop.to_json() == before
    assert not mobile.layout.title.text
    assert mobile.layout.showlegend is False
    assert mobile.layout.xaxis.rangeslider.visible is False
    assert mobile.data[0].mode == "lines"
    assert list(mobile.data[0].y) == list(desktop.data[0].y)
    selected = select_mobile_series(mobile.to_plotly_json(), [0], "12")
    assert selected["layout"]["xaxis"]["range"][0].startswith("2025-12")
    assert "range" not in mobile.to_plotly_json()["layout"]["xaxis"]


def test_fx_uses_one_currency_one_axis_and_preserves_rates():
    data = pd.DataFrame({"Дата": pd.date_range("2026-01-01", periods=3, freq="MS"),
                         "USD": [81., 83., 82.], "KZT": [.18, .19, .17]})
    desktop = _fx_changes_figure(data, "RUB")
    before = desktop.to_json()
    mobile = mobile_figure(desktop, "fx_changes")
    chosen = select_mobile_series(mobile.to_plotly_json(), 1)
    assert [t["visible"] for t in chosen["data"]] == [False, True]
    assert all(t["yaxis"] == "y" for t in chosen["data"])
    assert "yaxis2" not in chosen["layout"]
    assert list(mobile.data[1].y) == [.18, .19, .17]
    assert desktop.to_json() == before


@pytest.mark.parametrize("chart_id", ["month_cost_distribution_chart", "year_cost_distribution_chart", "planning_fx_scenarios"])
def test_categories_horizontal_but_values_and_hover_kept(chart_id):
    desktop = go.Figure(go.Bar(x=["Длинное название категории", "Пища"], y=[500, -20],
                               hovertemplate="%{x}<br>%{y:,.2f}"))
    mobile = mobile_figure(desktop, chart_id)
    assert mobile.data[0].orientation == "h"
    assert list(mobile.data[0].x) == [500, -20]
    assert mobile.data[0].hovertemplate == "%{y}<br>%{x:,.2f}"
    assert mobile.layout.xaxis.rangemode == "tozero"
    assert "<br>" in mobile.layout.yaxis.ticktext[0]
    assert desktop.data[0].orientation is None


def test_temporal_bars_do_not_rotate_or_aggregate():
    desktop = go.Figure(go.Bar(x=["2026-01-01", "2026-02-01"], y=[10, None], name="Пища"))
    mobile = mobile_figure(desktop, "expenses_monthly")
    assert mobile.data[0].orientation is None
    assert list(mobile.data[0].y) == [10, None]


def test_period_includes_future_forecast_and_preserves_isolated_points():
    fig = go.Figure([
        go.Scatter(x=["2026-01-01"], y=[100], name="Факт"),
        go.Scatter(x=["2026-01-01", "2027-01-01"], y=[100, 200], name="Прогноз"),
    ])
    mobile = mobile_figure(fig, "planning_capital_forecast")
    assert mobile.data[0].mode == "lines+markers"
    selected = select_mobile_series(mobile.to_plotly_json(), [0, 1], "36")
    assert selected["layout"]["xaxis"]["range"][0].startswith("2025-12")
    assert selected["layout"]["xaxis"]["range"][1].startswith("2027-01")


def test_pie_becomes_bars_without_losing_categories_values_or_colors():
    fig = go.Figure(go.Pie(labels=["Облигации", "Акции"], values=[100, 60],
                           marker_colors=["#338899", "#889933"]))
    mobile = mobile_figure(fig, "investment_allocation_type")
    assert mobile.data[0].type == "bar"
    assert list(mobile.data[0].x) == [100, 60]
    assert list(mobile.data[0].y) == ["Облигации", "Акции"]
    assert list(mobile.data[0].marker.color) == ["#338899", "#889933"]
    assert fig.data[0].type == "pie"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_t5_reuses_exact_currency_specific_gradient_and_amounts(theme):
    data = pd.DataFrame({"Счет": ["Большой", "Средний", "Маленький"],
                         "RUB": ["1000.00₽", "500.00₽", "1.00₽"],
                         "USD": ["10.00$", "5.00$", "0.01$"]})
    dataset = DashboardDataset(id="month_assets", title="Счета", dataframe=data)
    rows = _grid_row_data(dataset, data)
    maps = _report_table_style_maps(dataset, data)
    for currency in ("RUB", "USD"):
        table = currency_rows(currency, {"rows": rows, "maps": maps, "theme": theme, "locale": "ru"})
        cells = [row.children[1] for row in table.children[1].children]
        assert [cell.children for cell in cells] == list(data[currency])
        assert [cell.style for cell in cells] == [_report_cell_style(currency, row, maps, theme) for row in rows]
        assert len({cell.style["backgroundColor"] for cell in cells}) > 1


def test_t1_exact_fields_are_not_lost_when_summary_is_rounded():
    rows = [{"Год": "2026", "Доход": "6 123 456.78₽", "Расход": "123 456.70₽", "Сальдо": "6 000 000.08₽", "Источник": "Проверено"}]
    result = mobile_rows(rows, list(rows[0]), {}, "dark", "ru")
    detail = result.children[2]
    assert "6.12" == detail.children[0].children[1].children[0].children
    assert [item.children[1].children for item in detail.children[1].children] == list(rows[0].values())


def test_year_table_keeps_entire_history_and_uses_one_header():
    rows = [{"Год": str(y), "Доход": "100.00₽", "Расход": "50.00₽", "Сальдо": "50.00₽"}
            for y in range(2026, 2015, -1)]
    result = mobile_rows(rows, list(rows[0]), {}, "dark", "ru")
    details = result.children[2:]
    assert len(details) == 11
    assert [d.children[0].children[0].children for d in details] == [str(y) for y in range(2026, 2015, -1)]


def test_small_report_tables_are_not_replaced():
    from src.dashboard.app import _grid_section
    for dataset_id in ("year_income_cost_stats", "year_cost_by_month", "year_income_by_month"):
        data = pd.DataFrame({"Дата": ["2026-01"], "Доход": [100], "Расход": [50]})
        dataset = DashboardDataset(dataset_id, "Report", data)
        section = _grid_section(dataset)
        assert len(section.children) == 2
        assert "finrep-desktop-only" not in section.children[1].className


def test_all_supported_fx_can_be_requested_without_transaction_filter(monkeypatch):
    from src.data import exchange_rates_info as rates
    monkeypatch.setattr(rates, "get_transactions", lambda: pd.DataFrame({"Валюта": ["KZT", "USD"]}))
    monkeypatch.setattr(rates, "get_fx_rate_info", lambda *_: {"rate": 2., "change_pct": 0, "source": "synthetic"})
    assert set(rates.get_exchange_rates_info()["Валюта"]) == {"KZT", "USD"}
    result = rates.get_exchange_rates_info(currencies=["RUB", "USD", "EUR", "GBP", "KZT"])
    assert result["Валюта"].tolist() == ["EUR", "GBP", "KZT", "USD"]
    assert result.attrs["target_currency"] == "RUB"


def test_point_navigation_and_null_values():
    from src.dashboard.mobile_reports import point_readout
    figure = {"data": [{"name": "Капитал", "x": ["2026-01-01", "2026-02-01"], "y": [100, None]}]}
    date, details = point_readout(figure, None, "ru")
    assert date == "2026-02-01"
    assert details[1].children[1].children == "—"
    date, details = point_readout(figure, date, "ru", -1)
    assert date == "2026-01-01"
    assert details[1].children[1].children == "100.00"


def test_bars_labels_are_outside_tracks_and_negative_values_keep_direction():
    from src.dashboard.mobile_reports import bar_rows
    rows = bar_rows({"rows": [{"label": "Пища", "value": 100, "color": "blue"},
                              {"label": "Возврат", "value": -20, "color": "blue"}]}, "amount")
    assert rows[0].children[0].children[1].children == "100.00"
    assert rows[1].children[0].children[1].children == "-20.00"
    assert rows[1].children[1].children.style["marginLeft"] == "40.0%"
    assert rows[1].children[1].children.style["width"] == "10.0%"


@pytest.mark.parametrize("end,visible", [("2026-05-01", False), ("2027-01-01", False), ("2027-02-01", True)])
def test_period_controls_only_for_more_than_one_year(end, visible):
    from src.dashboard.mobile_reports import mobile_chart
    dates = ["2026-01-01", end]
    dataset = DashboardDataset("short", "History", pd.DataFrame(),
                               figure=go.Figure(go.Scatter(x=dates, y=[1, 2], name="Income")))
    chart = mobile_chart(dataset, "en")
    period = chart.children[1]
    assert (period.style.get("display") != "none") == visible
    assert not any(getattr(child, "className", "") == "finrep-mobile-details" for child in chart.children)


def test_cursor_dots_and_readout_move_together_without_mutating_source():
    from src.dashboard.mobile_reports import mobile_selection
    source = mobile_figure(go.Figure([
        go.Scatter(x=["2026-01-01", "2026-02-01"], y=[100, None], name="Income", line_color="blue"),
        go.Scatter(x=["2026-01-01", "2026-02-01"], y=[20, 30], name="Cost", line_color="red"),
    ]), "income").to_plotly_json()
    fig, date, readout = mobile_selection(source, [0, 1], "all", None, "en")
    assert date == fig["layout"]["shapes"][-1]["x0"] == "2026-02-01"
    assert readout[0].children[1].dateTime == date
    assert len(fig["data"]) == 3  # No marker for missing income.
    assert fig["data"][-1]["y"] == [30]
    fig, date, readout = mobile_selection(source, [0, 1], "all", date, "en", -1)
    assert date == fig["layout"]["shapes"][-1]["x1"] == "2026-01-01"
    assert [t["y"] for t in fig["data"][2:]] == [[100], [20]]
    assert [t["marker"]["color"] for t in fig["data"][2:]] == ["blue", "red"]
    assert len(source["data"]) == 2 and not source["layout"].get("shapes")
    fig, date, readout = mobile_selection(source, [], "all", date, "en")
    assert date is None and not fig["layout"].get("shapes")


def test_cursor_clamps_to_selected_period_and_only_selected_series():
    from src.dashboard.mobile_reports import mobile_selection
    dates = pd.date_range("2023-01-01", periods=48, freq="MS")
    source = mobile_figure(go.Figure(go.Scatter(x=dates, y=list(range(48)), name="Capital")), "capital").to_plotly_json()
    fig, date, _ = mobile_selection(source, [0], "12", "2023-01-01", "ru")
    assert date == "2026-12-01"
    assert fig["layout"]["shapes"][-1]["x0"] == date
    assert fig["data"][-1]["y"] == [47]
