"""Mobile-only presentation; original figures and export data stay untouched."""
from copy import deepcopy
import re

from dash import Input, MATCH, Output, State, ctx, dcc, html
import dash_bootstrap_components as dbc
import pandas as pd
import plotly.graph_objects as go

from src.dashboard.i18n import report_column_label
from src.dashboard.report_tables import _report_cell_style


CATEGORY_CHARTS = {"month_cost_distribution_chart", "year_cost_distribution_chart",
                   "planning_fx_scenarios"}


def mobile_figure(figure, dataset_id):
    fig = go.Figure(figure)
    fig.update_layout(title=None, showlegend=False, autosize=True, height=None, width=None,
                      margin=dict(l=48, r=16, t=16, b=42), font=dict(size=12),
                      hovermode="closest", dragmode=False)
    fig.layout.annotations = ()
    fig.update_xaxes(nticks=4, tickangle=0, automargin=True, fixedrange=True,
                     rangeslider_visible=False, title=None)
    fig.update_yaxes(nticks=4, automargin=True, fixedrange=True)
    palette = fig.layout.colorway or fig.layout.template.layout.colorway
    for index, trace in enumerate(fig.data):
        if trace.type == "scatter" and not (isinstance(trace.meta, dict) and trace.meta.get("user_comments")):
            trace.mode = "lines+markers" if pd.Series(trace.y).notna().sum() <= 2 else "lines"
            trace.text = None
            if not trace.line.color and palette:
                trace.line.color = palette[index % len(palette)]
        if trace.type == "bar" and trace.marker.color is None and palette:
            trace.marker.color = palette[index % len(palette)]
    if dataset_id == "fx_changes":
        for trace in fig.data:
            trace.yaxis = "y"
        fig.update_yaxes(title=None, autorange=True)
        fig.layout.yaxis2 = None
    if dataset_id in CATEGORY_CHARTS:
        for trace in fig.data:
            trace.x, trace.y = trace.y, trace.x
            trace.orientation = "h"
            trace.texttemplate = "%{x:,.3~s}"
            trace.textposition = "outside"
            trace.cliponaxis = False
            if trace.hovertemplate:
                trace.hovertemplate = trace.hovertemplate.replace("%{x", "%{TEMP").replace("%{y", "%{x").replace("%{TEMP", "%{y")
        fig.update_xaxes(type="linear", rangemode="tozero")
        fig.update_yaxes(type="category", autorange="reversed", title=None)
        # Long category names wrap rather than taking the entire phone width.
        labels = list(fig.data[0].y) if fig.data else []
        fig.update_yaxes(tickmode="array", tickvals=labels,
                         ticktext=[_wrap_label(str(label)) for label in labels])
        fig.update_layout(height=max(320, len(labels) * 54 + 60), margin=dict(l=120, r=52))
    if fig.data and fig.data[0].type == "pie":
        pie = fig.data[0]
        colors = pie.marker.colors
        if not colors and palette:
            colors = [palette[i % len(palette)] for i in range(len(pie.labels))]
        bars = go.Bar(x=pie.values, y=pie.labels, orientation="h",
                      marker=dict(color=colors) if colors else {},
                      texttemplate="%{x:,.3~s}", textposition="outside", cliponaxis=False,
                      hovertemplate="%{y}<br>%{x:,.2f}<extra></extra>")
        fig.data = []
        fig.add_trace(bars)
        fig.update_xaxes(rangemode="tozero")
        fig.update_yaxes(type="category", autorange="reversed", title=None,
                         tickmode="array", tickvals=list(bars.y),
                         ticktext=[_wrap_label(str(label)) for label in bars.y])
        fig.update_layout(height=max(320, len(bars.y) * 54 + 60), margin=dict(l=120, r=52))
    return fig


def _wrap_label(label):
    import textwrap
    from html import escape
    return "<br>".join(escape(line) for line in textwrap.wrap(label, width=17))


def mobile_chart(dataset, locale):
    fig = mobile_figure(dataset.figure, dataset.id)
    if fig.data and all(t.type == "bar" and t.orientation == "h" for t in fig.data):
        return mobile_bars(dataset, fig, locale)
    options = []
    for index, trace in enumerate(fig.data):
        if trace.showlegend is False or not trace.name:
            continue
        color = (trace.line.color if trace.type == "scatter" else trace.marker.color)
        if not isinstance(color, str):
            color = "#709fbd"
        options.append({"label": html.Span([
            html.Span(className="finrep-series-swatch", style={"backgroundColor": color}, **{"aria-hidden": "true"}),
            " " + trace.name,
        ]), "value": index})
    fx = dataset.id == "fx_changes"
    selected = [o["value"] for o in options]
    selection = selected[:1] if fx else selected
    controls = dcc.RadioItems if fx else dcc.Checklist
    temporal = any(trace.type == "scatter" for trace in fig.data) or (
        fig.data and fig.data[0].type == "bar" and fig.data[0].orientation != "h")
    dates = pd.to_datetime([x for trace in fig.data for x in trace.x], errors="coerce").dropna()
    show_period = temporal and len(dates) > 0 and dates.max() > dates.min() + pd.DateOffset(years=1)
    initial_period = "36" if show_period else "all"
    selected_figure, date, readout = mobile_selection(fig.to_plotly_json(), selection, initial_period, None, locale)
    return html.Div([
        dcc.Store(id={"type": "mobile-chart-source", "index": dataset.id}, data=fig.to_plotly_json()),
        dcc.RadioItems(id={"type": "mobile-chart-period", "index": dataset.id},
                       options=[{"label": "1Y" if locale == "en" else "1 год", "value": "12"},
                                {"label": "3Y" if locale == "en" else "3 года", "value": "36"},
                                {"label": "All" if locale == "en" else "Всё", "value": "all"}],
                       value=initial_period, inline=True, className="finrep-mobile-segments",
                       style={} if show_period else {"display": "none"}),
        controls(id={"type": "mobile-chart-series", "index": dataset.id},
                 options=options, value=(selection[0] if selection else None) if fx else selection,
                 className="finrep-mobile-segments" if fx else "finrep-mobile-legend", inline=True,
                 style={} if len(options) > 1 else {"display": "none"}),
        dcc.Graph(id={"type": "mobile-chart", "index": dataset.id},
                  figure=selected_figure, responsive=True,
                  config={"displayModeBar": False, "responsive": True, "scrollZoom": False},
                  style={"height": f"{fig.layout.height or 340}px", "width": "100%"}),
        dcc.Store(id={"type": "mobile-chart-point", "index": dataset.id}, data=date),
        html.Div(readout,
                 id={"type": "mobile-chart-readout", "index": dataset.id},
                 className="finrep-mobile-readout", **{"aria-live": "polite"}),
        html.Div([
            html.Button("← Month" if locale == "en" else "← Месяц", id={"type": "mobile-point-prev", "index": dataset.id}),
            html.Button("Month →" if locale == "en" else "Месяц →", id={"type": "mobile-point-next", "index": dataset.id}),
        ], className="finrep-mobile-point-nav"),
        dcc.Store(id={"type": "mobile-chart-locale", "index": dataset.id}, data=locale),
    ], className="finrep-mobile-only finrep-mobile-chart")


def select_mobile_series(figure, selected, period="all"):
    result = deepcopy(figure)
    chosen = selected if isinstance(selected, list) else [selected]
    for index, trace in enumerate(result["data"]):
        if trace.get("name") and trace.get("showlegend") is not False:
            trace["visible"] = index in chosen
    if period != "all":
        dates = pd.to_datetime([date for trace in result["data"] for date in trace.get("x", [])], errors="coerce")
        if len(dates) and dates.notna().any():
            end = dates.max() + pd.Timedelta(days=15)
            start = max(dates.min() - pd.Timedelta(days=15), end - pd.DateOffset(months=int(period)))
            result["layout"]["xaxis"].update(range=[start.isoformat(), end.isoformat()], autorange=False)
    return result


def point_readout(figure, date, locale, step=0):
    traces = [t for t in figure["data"] if t.get("visible", True) not in (False, "legendonly")
              and t.get("showlegend") is not False]
    dates = sorted({str(x)[:10] for t in traces for x in t.get("x", [])})
    limits = figure.get("layout", {}).get("xaxis", {}).get("range")
    if limits:
        dates = [d for d in dates if str(limits[0])[:10] <= d <= str(limits[1])[:10]]
    if not dates:
        return None, "No selected data" if locale == "en" else "Нет выбранных данных"
    index = dates.index(date) if date in dates else len(dates) - 1
    date = dates[max(0, min(len(dates) - 1, index + step))]
    rows = []
    for trace in traces:
        values = [y for x, y in zip(trace.get("x", []), trace.get("y", [])) if str(x)[:10] == date]
        value = values[-1] if values else None
        text = "—" if value is None or pd.isna(value) else f"{float(value):,.4f}" if "/" in trace.get("name", "") else f"{float(value):,.2f}"
        color = trace.get("line", {}).get("color") or trace.get("marker", {}).get("color")
        rows.append(html.Div([
            html.Span([html.Span(className="finrep-readout-swatch", style={"backgroundColor": color if isinstance(color, str) else "#709fbd"},
                                **{"aria-hidden": "true"}), trace.get("name", "")]),
            html.B(text.replace(",", " ")),
        ], className="finrep-readout-row is-zero" if value == 0 else "finrep-readout-row"))
    timestamp = pd.Timestamp(date)
    months = ("January February March April May June July August September October November December" if locale == "en" else
              "Январь Февраль Март Апрель Май Июнь Июль Август Сентябрь Октябрь Ноябрь Декабрь").split()
    return date, [html.Div([
        html.Small("Selected month" if locale == "en" else "Выбранный месяц"),
        html.Time(f"{months[timestamp.month - 1]} {timestamp.year}", dateTime=date),
    ], className="finrep-readout-heading"), *rows]


def mobile_selection(source, selected, period, date, locale, step=0):
    figure = select_mobile_series(source, selected, period)
    date, readout = point_readout(figure, date, locale, step)
    if date:
        figure["layout"].setdefault("shapes", []).append(dict(
            type="line", x0=date, x1=date, xref="x", y0=0, y1=1, yref="paper",
            line=dict(color="#94a3b8", width=1, dash="dash")))
        markers = []
        for trace in figure["data"]:
            if trace.get("type") != "scatter" or trace.get("visible") is False or trace.get("showlegend") is False:
                continue
            for x, y in zip(trace.get("x", []), trace.get("y", [])):
                if str(x)[:10] == date and y is not None and pd.notna(y):
                    markers.append(dict(type="scatter", x=[x], y=[y], mode="markers", showlegend=False,
                                        yaxis=trace.get("yaxis", "y"), hoverinfo="skip",
                                        marker=dict(size=9, color=trace.get("line", {}).get("color", "#709fbd"))))
        figure["data"].extend(markers)
    return figure, date, readout


def bar_rows(payload, mode):
    rows = payload["rows"]
    total = sum(row["value"] for row in rows)
    maximum = max([abs(row["value"]) for row in rows] or [1]) or 1
    mixed = any(row["value"] < 0 for row in rows)
    result = []
    for row in rows:
        value = row["value"]
        display = f"{value / total * 100:.2f}%" if mode == "share" and total > 0 else f"{value:,.2f}".replace(",", " ")
        width = abs(value) / maximum * (50 if mixed else 100)
        start = 50 - width if value < 0 else 50 if mixed else 0
        result.append(html.Div([
            html.Div([html.Span(row["label"]), html.B(display)], className="finrep-bar-label"),
            html.Div(html.Div(style={"width": f"{width}%", "marginLeft": f"{start}%", "backgroundColor": row["color"]}),
                     className="finrep-bar-track is-diverging" if mixed else "finrep-bar-track"),
        ], className="finrep-bar-row"))
    return result


def mobile_bars(dataset, figure, locale):
    rows = []
    for trace in figure.data:
        for i, (label, value) in enumerate(zip(trace.y, trace.x)):
            color = trace.marker.color
            color = color[i] if color is not None and not isinstance(color, str) else color
            if value is not None and pd.notna(value):
                rows.append({"label": str(label), "value": float(value), "color": color or "#709fbd"})
    payload = {"rows": rows}
    shares = sum(r["value"] for r in rows) > 0 and dataset.id != "planning_fx_scenarios"
    return html.Div([
        dcc.Store(id={"type": "mobile-bars-source", "index": dataset.id}, data=payload),
        dcc.RadioItems(id={"type": "mobile-bars-mode", "index": dataset.id},
                       options=[{"label": "Amounts" if locale == "en" else "Суммы", "value": "amount"},
                                {"label": "Shares" if locale == "en" else "Доли", "value": "share"}],
                       value="amount", className="finrep-mobile-segments", style={} if shares else {"display": "none"}),
        html.P("Report currency · all categories" if locale == "en" else "В валюте отчёта · все категории", className="small"),
        html.Div(bar_rows(payload, "amount"), id={"type": "mobile-bars", "index": dataset.id}),
    ], className="finrep-mobile-only finrep-mobile-chart finrep-mobile-bars")


def _millions(value):
    text = str(value)
    match = re.fullmatch(r"([+−-]?[\d\s,]+(?:\.\d+)?)([₽$€£₸]?)", text)
    if not match:
        return text
    number = float(match[1].replace(" ", "").replace("\u00a0", "").replace(",", "").replace("−", "-"))
    return f"{number / 1_000_000:.2f}"


def mobile_rows(rows, columns, style_maps, theme, locale):
    key = columns[0]
    preferred = [c for c in ("Доход", "Расход", "Сальдо") if c in columns]
    primary = preferred or columns[1:3]
    symbols = {str(row.get(c, ""))[-1:] for row in rows for c in primary} & set("₽$€£₸")
    unit = next(iter(symbols)) if len(symbols) == 1 else ""
    return html.Div([
        html.P(f"Millions {unit} · rounded. Tap a year for exact amounts and details." if locale == "en"
               else f"млн {unit} · округлено до 0,01 млн. Нажми год — точные суммы и остальные показатели.", className="small"),
        html.Div([html.Span(report_column_label(c, locale)) for c in [key, *primary]], className="finrep-mobile-summary finrep-mobile-table-head"),
        *[html.Details([
            html.Summary([
                html.Strong(str(row.get(key, ""))),
                *[html.Span([
                    html.Span(_millions(row.get(column, "")), className="finrep-mobile-number"),
                ], className="is-positive" if column == "Доход" else "is-negative" if column == "Расход" else "") for column in primary],
            ], className="finrep-mobile-summary", style={"gridTemplateColumns": f"minmax(0, .8fr) repeat({len(primary)}, minmax(0, 1fr))"}),
            html.Dl([html.Div([html.Dt(report_column_label(column, locale)),
                              html.Dd(str(row.get(column, "")))],
                             style=_report_cell_style(column, row, style_maps, theme))
                     for column in columns], className="finrep-mobile-fields"),
        ], className="finrep-mobile-details") for row in rows],
    ], className="finrep-mobile-only finrep-mobile-rows")


def mobile_fx(rows, locale, change_class):
    rows = [dict(row) for row in rows]
    for row in rows:
        if str(row["Курс"]) in {"None", "nan", "N/A", "Ошибка", ""}:
            row["Курс"] = "No data" if locale == "en" else "Нет данных"
    return html.Div([
        html.P("All supported foreign currencies · tap for source and inverse rate" if locale == "en" else "Все поддерживаемые иностранные валюты · нажми строку для источника и обратного курса", className="small"),
        html.Div([html.Span(report_column_label(c, locale)) for c in ["Валюта", "Курс", "Изм."]],
                 className="finrep-mobile-summary finrep-mobile-table-head", style={"gridTemplateColumns": "minmax(0, .7fr) repeat(2, minmax(0, 1fr))"}),
        *[html.Details([
        html.Summary([html.Strong(row["Валюта"]), html.Span(str(row["Курс"])),
                      html.Span(str(row["Изменение (%)"]), className=change_class(row["Изменение (%)"]))], className="finrep-mobile-summary",
                     style={"gridTemplateColumns": "minmax(0, .7fr) repeat(2, minmax(0, 1fr))"}),
        html.Dl([html.Div([html.Dt(report_column_label(column, locale)), html.Dd(str(row.get(column, "")))])
                 for column in ("Обратный курс", "Источник")], className="finrep-mobile-fields"),
    ], className="finrep-mobile-details") for row in rows]], className="finrep-mobile-only finrep-mobile-fx")


def currency_rows(currency, payload):
    return html.Table([
        html.Thead(html.Tr([html.Th(report_column_label("Счет", payload["locale"])), html.Th(currency)])),
        html.Tbody([html.Tr([
            html.Td(row["Счет"]),
            html.Td(row.get(currency, ""), style=_report_cell_style(currency, row, payload["maps"], payload["theme"])),
        ]) for row in payload["rows"]]),
    ], className="finrep-report-table finrep-mobile-currency-table")


def mobile_assets(rows, columns, style_maps, theme, locale, matrix):
    currencies = columns[1:]
    default = "RUB" if "RUB" in currencies else currencies[0]
    payload = {"rows": rows, "maps": style_maps, "theme": theme, "locale": locale}
    return html.Div([
        dcc.Store(id="mobile-assets-source", data=payload),
        dcc.RadioItems(id="mobile-assets-currency", options=currencies, value=default,
                       className="finrep-mobile-segments", inline=True),
        html.Div(currency_rows(default, payload), id="mobile-assets-values"),
        html.Button("Expand full matrix ↗" if locale == "en" else "Развернуть полную матрицу ↗",
                    id="mobile-assets-expand", className="finrep-mobile-expand"),
        dbc.Modal([
            dbc.ModalHeader(dbc.ModalTitle("Account allocation" if locale == "en" else "Распределение по счетам",
                                           id="mobile-assets-modal-title"), close_button=False),
            dbc.ModalBody(html.Div(matrix, className="finrep-mobile-matrix", tabIndex=0)),
            dbc.ModalFooter(html.Button("Close" if locale == "en" else "Закрыть", id="mobile-assets-close", className="finrep-mobile-expand")),
        ], id="mobile-assets-modal", is_open=False, fullscreen=True, labelledby="mobile-assets-modal-title",
           className=f"finrep-mobile-matrix-modal finrep-theme-{theme or 'dark'}"),
    ], className="finrep-mobile-only")


def register_mobile_callbacks(app):
    @app.callback(Output("mobile-assets-modal", "is_open"),
                  Input("mobile-assets-expand", "n_clicks"), Input("mobile-assets-close", "n_clicks"), prevent_initial_call=True)
    def expand_matrix(_open, _close):
        return ctx.triggered_id == "mobile-assets-expand"

    @app.callback(Output({"type": "mobile-bars", "index": MATCH}, "children"),
                  Input({"type": "mobile-bars-mode", "index": MATCH}, "value"),
                  State({"type": "mobile-bars-source", "index": MATCH}, "data"), prevent_initial_call=True)
    def select_bar_mode(mode, payload):
        return bar_rows(payload, mode)

    @app.callback(Output({"type": "mobile-chart-point", "index": MATCH}, "data"),
                  Output({"type": "mobile-chart-readout", "index": MATCH}, "children"),
                  Output({"type": "mobile-chart", "index": MATCH}, "figure"),
                  Input({"type": "mobile-chart-series", "index": MATCH}, "value"),
                  Input({"type": "mobile-chart-period", "index": MATCH}, "value"),
                  Input({"type": "mobile-chart", "index": MATCH}, "clickData"),
                  Input({"type": "mobile-point-prev", "index": MATCH}, "n_clicks"),
                  Input({"type": "mobile-point-next", "index": MATCH}, "n_clicks"),
                  State({"type": "mobile-chart-point", "index": MATCH}, "data"),
                  State({"type": "mobile-chart-source", "index": MATCH}, "data"),
                  State({"type": "mobile-chart-locale", "index": MATCH}, "data"), prevent_initial_call=True)
    def select_point(selected, period, click, _prev, _next, date, source, locale):
        trigger = ctx.triggered_id.get("type")
        step = -1 if trigger == "mobile-point-prev" else 1 if trigger == "mobile-point-next" else 0
        if trigger == "mobile-chart" and ctx.triggered[0]["prop_id"].endswith(".clickData") and click:
            date = str(click["points"][0]["x"])[:10]
        figure, date, readout = mobile_selection(source, selected, period, date, locale, step)
        return date, readout, figure

    @app.callback(Output("mobile-assets-values", "children"),
                  Input("mobile-assets-currency", "value"), State("mobile-assets-source", "data"),
                  prevent_initial_call=True)
    def select_currency(currency, payload):
        return currency_rows(currency, payload)
