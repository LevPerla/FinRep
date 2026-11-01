"""Opt-in real Plotly geometry regression: pytest tests/browser_mobile_reports.py.

Requires installed Playwright Chromium (or FINREP_TEST_CHROMIUM executable).
Uses synthetic data and a loopback-only server; no private database or PDFs.
"""
import os
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from dash import Dash, html
from flask import request
import pandas as pd
from playwright.sync_api import sync_playwright
import plotly.graph_objects as go
import pytest
from werkzeug.serving import make_server

from src.dashboard.app import _graph_section, _grid_section
from src.dashboard.main_data import DashboardDataset, _fx_changes_figure
from src.dashboard.mobile_reports import register_mobile_callbacks


@pytest.fixture(scope="module")
def mobile_page():
    app = Dash(__name__, assets_folder=str(Path(__file__).resolve().parents[1] / "assets"),
               suppress_callback_exceptions=True,
               meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}])
    dates = pd.date_range("2023-01-01", periods=48, freq="MS")
    capital = pd.DataFrame({"Дата": dates, "Капитал": range(48)})
    figure = go.Figure()
    for i in range(4):
        figure.add_scatter(x=dates, y=[v * (i + 1) for v in range(48)],
                           name=f"Очень длинное название капитала и источника оценки {i}",
                           mode="lines+markers+text", text=[str(v) for v in range(48)])
    figure.update_layout(title="Дублированный заголовок", legend=dict(y=1.02))
    rates = pd.DataFrame({"Дата": dates, "USD": [80 + n / 10 for n in range(48)],
                         "KZT": [.18 + n / 10000 for n in range(48)]})
    categories = pd.DataFrame({"Категория": ["Дом и хозяйственные принадлежности", "Пища", "Транспорт"],
                               "Суммарно": [12000, 5000, 3000]})
    assets = pd.DataFrame({"Счет": ["Большой вклад с длинным названием KZT", "Средний вклад", "Наличные"],
                           "RUB": ["24 368 393.08₽", "5 000 000.00₽", "100.00₽"],
                           "USD": ["300 000.00$", "61 000.00$", "1.25$"]})
    years = pd.DataFrame({"Год": [str(y) for y in range(2026, 2015, -1)], "Доход": [6123456.78] * 11,
                          "Расход": [2100000.] * 11, "Сальдо": [4023456.78] * 11,
                          "Дополнительное поле": ["Не теряется"] * 11})
    display = years.copy()
    for col in ("Доход", "Расход", "Сальдо"):
        display[col] = display[col].map(lambda value: f"{value:,.2f}₽".replace(",", " "))
    fx = pd.DataFrame({"Валюта": ["USD", "EUR", "GBP", "KZT"], "Курс": ["81.2463"] * 4, "Обратный курс": ["0.0123"] * 4,
                       "Изменение (%)": ["+0.00%"] * 4, "Источник": ["Synthetic fixture · 2026-10-31"] * 4})

    def layout():
        # Dash requests /_dash-layout separately; use the page's query via Referer.
        params = parse_qs(urlparse(request.referrer or request.url).query) if request else {}
        theme = params.get("theme", ["dark"])[0]
        locale = params.get("locale", ["ru"])[0]
        datasets = [DashboardDataset("capital", "Динамика капитала", capital, figure=figure),
                    DashboardDataset("fx_changes", "Курсы валют", rates, figure=_fx_changes_figure(rates, "RUB")),
                    DashboardDataset("month_cost_distribution_chart", "Расходы по категориям", categories,
                                     figure=go.Figure(go.Bar(x=categories["Категория"], y=categories["Суммарно"]))),
                    DashboardDataset("delta", "Денежный поток", capital,
                                     figure=go.Figure(go.Bar(x=dates, y=list(range(48)), name="Единственный ряд"))),
                    DashboardDataset("short", "Годовой отчёт", capital.iloc[:6],
                                     figure=go.Figure(go.Scatter(x=dates[:6], y=list(range(6)), name="Доход")))]
        sections = [_graph_section(d, theme=theme, locale=locale) for d in datasets]
        sections += [_grid_section(d, theme=theme, locale=locale) for d in [
            DashboardDataset("month_assets", "Счета", assets),
            DashboardDataset("yearly_stats", "Итоги по годам", years, display_dataframe=display),
            DashboardDataset("fx_rates", "Курсы", fx),
        ]]
        return html.Div(sections, className=f"finrep-theme-{theme}",
                        style={"padding": "12px", "background": "#292929" if theme == "dark" else "#fff",
                               "color": "#eee" if theme == "dark" else "#222", "fontFamily": "Arial"})

    app.layout = layout
    register_mobile_callbacks(app)
    server = make_server("127.0.0.1", 0, app.server, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("FINREP_TEST_CHROMIUM"))
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page, f"http://127.0.0.1:{server.server_port}", errors
        browser.close()
    server.shutdown()
    thread.join()


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("locale", ["ru", "en"])
def test_mobile_geometry_and_desktop_restoration(mobile_page, theme, locale):
    page, url, errors = mobile_page
    page.goto(f"{url}/?theme={theme}&locale={locale}")
    page.wait_for_selector(".finrep-mobile-chart .js-plotly-plot")
    assert page.locator(".finrep-mobile-chart .finrep-mobile-details summary").count() == 0
    for width in (320, 360, 390, 430, 767, 768, 1280, 1440, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(500)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
        for section in page.locator(".finrep-chart-section").all():
            header = section.locator(".finrep-section-header").bounding_box()
            if width <= 768:
                if section.locator(".finrep-mobile-bars").count():
                    for row in section.locator(".finrep-bar-row").all():
                        label = row.locator(".finrep-bar-label").bounding_box()
                        track = row.locator(".finrep-bar-track").bounding_box()
                        assert label["y"] + label["height"] + 4 <= track["y"]
                        assert label["x"] + label["width"] <= width
                else:
                    plot = section.locator(".finrep-mobile-chart .js-plotly-plot").bounding_box()
                    for control in section.locator(".finrep-mobile-legend:visible, .finrep-mobile-segments:visible").all():
                        bounds = control.bounding_box()
                        assert header["y"] + header["height"] <= bounds["y"] + 1
                        assert bounds["y"] + bounds["height"] <= plot["y"] + 1
                    for label in section.locator(".finrep-mobile-legend:visible label").all():
                        assert label.evaluate("e => getComputedStyle(e).color") == (
                            "rgb(220, 220, 220)" if theme == "dark" else "rgb(33, 37, 41)")
                        assert label.locator(".finrep-series-swatch").bounding_box()["width"] >= 16
                    for row in section.locator(".finrep-readout-row").all():
                        label = row.locator(":scope > span").bounding_box()
                        value = row.locator("b").bounding_box()
                        assert label["x"] + label["width"] + 4 <= value["x"]
                        assert value["x"] + value["width"] <= width
                assert section.locator(".finrep-mobile-chart .gtitle").count() == 0
                assert not section.locator(".finrep-desktop-only").is_visible()
            else:
                assert section.locator(".finrep-desktop-only").is_visible()
                assert not section.locator(".finrep-mobile-chart").is_visible()
        assert page.locator("#capital-graph .js-plotly-plot").evaluate("p => p.layout.title.text") == "Дублированный заголовок"
    assert errors == []


def test_mobile_controls_details_and_gradients(mobile_page):
    page, url, errors = mobile_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    page.wait_for_selector("#mobile-assets-values")
    assert page.locator(".finrep-mobile-rows details").count() == 11
    assert page.locator(".finrep-mobile-fx details").count() == 4
    single = page.locator("#delta-graph").locator("xpath=../..")
    assert not single.locator(".finrep-mobile-legend").is_visible()
    capital = page.locator(".finrep-mobile-chart").first
    short = page.locator("#short-graph").locator("xpath=../..")
    assert not short.locator(".finrep-mobile-segments").is_visible()
    before = capital.locator(".finrep-mobile-readout time").get_attribute("datetime")
    capital.get_by_role("button", name="← Месяц").click()
    page.wait_for_function("document.querySelector('.finrep-mobile-readout time').getAttribute('datetime') !== " + repr(before))
    date = capital.locator("time").get_attribute("datetime")
    assert capital.locator(".js-plotly-plot").evaluate("p => p.layout.shapes.at(-1).x0") == date
    assert capital.locator(".js-plotly-plot").evaluate("p => p.data.slice(4).every(t => String(t.x[0]).slice(0,10) === p.layout.shapes.at(-1).x0)")
    capital.locator(".js-plotly-plot").evaluate("p => p.emit('plotly_click', {points:[{curveNumber:0, pointNumber:35, x:'2025-12-01', y:35}]})")
    page.wait_for_function("document.querySelector('.finrep-mobile-readout time').getAttribute('datetime') === '2025-12-01'")
    assert capital.locator(".js-plotly-plot").evaluate("p => p.layout.shapes.at(-1).x0") == "2025-12-01"
    fx = page.locator(".finrep-mobile-chart").nth(1)
    fx.get_by_text("KZT/RUB", exact=False).click()
    page.wait_for_function("""() => {
        const p = document.querySelectorAll('.finrep-mobile-chart .js-plotly-plot')[1];
        return p.data[1].visible === true && p.data[0].visible === false;
    }""")
    page.locator("#mobile-assets-currency").get_by_text("USD", exact=True).click()
    page.wait_for_function("document.querySelector('#mobile-assets-values').textContent.includes('300 000.00$')")
    colors = page.locator("#mobile-assets-values tbody td:last-child").evaluate_all("cells => cells.map(c => getComputedStyle(c).backgroundColor)")
    assert len(set(colors)) > 1
    summary = page.locator(".finrep-mobile-rows summary").first
    summary.focus()
    page.keyboard.press("Enter")
    assert page.locator(".finrep-mobile-rows details[open]").count() == 1
    assert "6 123 456.78₽" in page.locator(".finrep-mobile-rows details[open]").inner_text()
    page.set_viewport_size({"width": 320, "height": 844})
    page.wait_for_timeout(300)
    assert page.locator(".finrep-mobile-rows details[open]").count() == 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("#mobile-assets-expand").click()
    page.wait_for_selector("#mobile-assets-modal")
    assert page.locator(".finrep-mobile-matrix-modal").get_attribute("role") == "dialog"
    assert "300 000.00$" in page.locator("#mobile-assets-modal").inner_text()
    assert page.locator("#mobile-assets-modal").bounding_box()["width"] <= 320
    page.keyboard.press("Escape")
    page.wait_for_selector("#mobile-assets-modal", state="hidden")
    assert page.locator("#mobile-assets-expand").evaluate("e => e === document.activeElement")
    assert errors == []
