from __future__ import annotations

import pandas as pd

from src import config
from src.dashboard.main_data import DashboardDataset


def _report_table_style_maps(dataset: DashboardDataset, data: pd.DataFrame) -> dict[str, dict]:
    if dataset.id == "yearly_stats":
        return {
            "levels": {
                "Доход": ("__income_level", "green"),
                "Расход": ("__expense_level", "red"),
            },
            "signs": {
                "Сальдо": "__balance_sign",
                "Процент дохода": "__income_pct_sign",
            },
        }
    if dataset.id == "year_quarter_stats":
        return {
            "levels": {
                "Общий доход": ("__quarter_income_level", "green"),
                "Общий расход": ("__quarter_expense_level", "red"),
            },
            "signs": {"Сальдо": "__quarter_balance_sign"},
        }
    if dataset.id == "year_income_cost_stats":
        return {
            "levels": {
                "Доход": ("__stats_income_level", "green"),
                "Расход": ("__stats_expense_level", "red"),
            },
            "signs": {},
        }
    if dataset.id in {"year_cost_distribution", "month_cost_distribution"}:
        return {
            "levels": {
                "Суммарно": ("__cost_sum_level", "red"),
                "Среднее": ("__cost_avg_level", "red"),
                "Процент": ("__cost_pct_level", "red"),
            },
            "signs": {},
        }
    if dataset.id == "month_transactions":
        expense_columns = [column for column in data.columns if column not in ["Дата", *config.NOT_COST_COLS]]
        levels = {
            "Доход": ("__month_income_level", "green"),
            "Сбережения": ("__month_savings_level", "green"),
        }
        levels.update({column: (f"__month_expense_{index}_level", "red") for index, column in enumerate(expense_columns)})
        return {"levels": levels, "signs": {}}
    if dataset.id == "month_assets":
        return {"levels": {}, "signs": {}, "assets": {}}
    if dataset.id == "year_income_by_month":
        return {"levels": {"Доход": ("__monthly_income_level", "green")}, "signs": {}}
    if dataset.id == "year_cost_by_month":
        return {"levels": {"Расход": ("__monthly_cost_level", "red")}, "signs": {}}
    if dataset.id == "year_capital_by_month":
        return {
            "levels": {
                "Капитал": ("__monthly_capital_level", "green"),
                "Капитал по активам": ("__monthly_asset_capital_level", "blue"),
            },
            "signs": {
                "Валютная переоценка": "__monthly_fx_revaluation_sign",
                "Расхождение с активами": "__monthly_asset_gap_sign",
            },
        }
    if dataset.id == "planning_fx_scenarios":
        return {
            "levels": {"Капитал": ("__planning_fx_capital_level", "blue")},
            "signs": {"Изменение капитала": "__planning_fx_delta_sign"},
        }
    return {"levels": {}, "signs": {}}


def _report_row_class(row: dict) -> str:
    classes = []
    if row.get("__is_total") or row.get("__is_asset_total"):
        classes.append("is-total")
    if row.get("__is_asset_currency_total"):
        classes.append("is-currency-total")
    return " ".join(classes)


def _report_cell_class(column: str, row: dict) -> str:
    classes = ["finrep-report-cell"]
    if column in {"Год", "Квартал", "Дата", "Показатель", "Счет"}:
        classes.append("is-label")
    if row.get("__is_total") or row.get("__is_asset_total"):
        classes.append("is-total")
    if row.get("__is_asset_currency_total"):
        classes.append("is-currency-total")
    return " ".join(classes)


def _report_cell_style(column: str, row: dict, style_maps: dict, theme: str | None = None) -> dict:
    if row.get("__is_total") or row.get("__is_asset_total"):
        return _total_style(theme)

    if row.get("__is_asset_currency_total") and column != "Счет":
        return _level_cell_style(row.get(f"__asset_blue_{column}_level", 0), "blue", theme)

    if "assets" in style_maps and column != "Счет":
        return _level_cell_style(row.get(f"__asset_{column}_level", 0), "green", theme)

    level_info = style_maps.get("levels", {}).get(column)
    if level_info:
        field, palette = level_info
        return _level_cell_style(row.get(field, 0), palette, theme)

    sign_field = style_maps.get("signs", {}).get(column)
    if sign_field:
        return _sign_cell_style(row.get(sign_field), theme)

    return {}


def _grid_row_data(dataset: DashboardDataset, data: pd.DataFrame) -> list[dict]:
    display_data = _fill_zero_display_cells(data) if dataset.id in {"month_transactions", "month_summary"} else data
    records = display_data.to_dict("records")
    raw = dataset.dataframe.reset_index(drop=True)

    if dataset.id == "yearly_stats":
        income_max = _positive_max(raw[raw["Год"].astype(str) != "Всего"], "Доход")
        expense_max = _positive_max(raw[raw["Год"].astype(str) != "Всего"], "Расход")

        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            is_total = str(row.get("Год")) == "Всего"
            record["__is_total"] = is_total
            record["__income_level"] = 0 if is_total else _gradient_level(row.get("Доход"), income_max)
            record["__expense_level"] = 0 if is_total else _gradient_level(row.get("Расход"), expense_max)
            record["__balance_sign"] = "total" if is_total else _value_sign(row.get("Сальдо"))
            record["__income_pct_sign"] = "total" if is_total else _percentage_sign(row.get("Процент дохода"))
        return records

    if dataset.id == "year_quarter_stats":
        total_mask = raw["Квартал"].astype(str) == "Всего"
        _add_level_metadata(records, raw[~total_mask].reset_index(drop=True), {"Общий доход": "__quarter_income_level"})
        _add_level_metadata(records, raw[~total_mask].reset_index(drop=True), {"Общий расход": "__quarter_expense_level"})
        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            is_total = str(row.get("Квартал")) == "Всего"
            record["__is_total"] = is_total
            if is_total:
                record["__quarter_income_level"] = 0
                record["__quarter_expense_level"] = 0
                record["__quarter_balance_sign"] = "total"
            else:
                record["__quarter_balance_sign"] = _value_sign(row.get("Сальдо"))
        return records

    if dataset.id == "year_income_cost_stats":
        _add_level_metadata(records, raw, {"Доход": "__stats_income_level", "Расход": "__stats_expense_level"})
        return records

    if dataset.id in {"year_cost_distribution", "month_cost_distribution"}:
        _add_level_metadata(
            records,
            raw,
            {
                "Суммарно": "__cost_sum_level",
                "Среднее": "__cost_avg_level",
                "Процент": "__cost_pct_level",
            },
        )
        return records

    if dataset.id == "month_transactions":
        _add_level_metadata(records, raw, {"Доход": "__month_income_level", "Сбережения": "__month_savings_level"})
        expense_columns = [column for column in raw.columns if column not in ["Дата", *config.NOT_COST_COLS]]
        _add_level_metadata(records, raw, {column: f"__month_expense_{index}_level" for index, column in enumerate(expense_columns)})
        return records

    if dataset.id == "month_summary":
        _add_level_metadata(records, raw, {"Доход": "__month_summary_income_level", "Сбережения": "__month_summary_savings_level"})
        _add_level_metadata(records, raw, {"Расход": "__month_summary_expense_level"})
        _add_sign_metadata(
            records,
            raw,
            {
                "Валютная переоценка": "__month_summary_fx_sign",
                "Расхождение с активами": "__month_summary_asset_gap_sign",
            },
        )
        return records

    if dataset.id == "month_assets":
        _add_asset_metadata(records, raw)
        return records

    if dataset.id == "planning_goals":
        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            record["__planning_delta_sign"] = _value_sign(row.get("Отклонение"))
            record["__planning_progress_level"] = _gradient_level(row.get("Прогресс (%)"), 100)
        return records

    if dataset.id == "planning_fx_scenarios":
        _add_level_metadata(records, raw, {"Капитал": "__planning_fx_capital_level"})
        for index, record in enumerate(records):
            if index < len(raw):
                record["__planning_fx_delta_sign"] = _value_sign(raw.iloc[index].get("Изменение капитала"))
        return records

    if dataset.id == "investment_summary":
        for index, record in enumerate(records):
            if index < len(raw):
                record["__investment_summary_sign"] = _value_sign(raw.iloc[index].get("value"))
        return records

    if dataset.id == "investment_positions":
        _add_level_metadata(records, raw, {"market_value": "__investment_value_level", "allocation": "__investment_allocation_level"})
        for index, record in enumerate(records):
            if index < len(raw):
                row = raw.iloc[index]
                record["__investment_unrealized_sign"] = _value_sign(row.get("unrealized_pnl"))
                record["__investment_realized_sign"] = _value_sign(row.get("realized_pnl"))
                record["__investment_total_sign"] = _value_sign(row.get("total_pnl"))
        return records

    if dataset.id in {"investment_allocation_type", "investment_allocation_currency"}:
        _add_level_metadata(records, raw, {"market_value": "__investment_alloc_value_level", "allocation": "__investment_alloc_pct_level"})
        return records

    simple_level_tables = {
        "year_income_by_month": {"Доход": ("__monthly_income_level", "green")},
        "year_cost_by_month": {"Расход": ("__monthly_cost_level", "red")},
        "year_capital_by_month": {
            "Капитал": ("__monthly_capital_level", "green"),
            "Капитал по активам": ("__monthly_asset_capital_level", "blue"),
        },
    }
    if dataset.id in simple_level_tables:
        _add_level_metadata(
            records,
            raw,
            {column: field for column, (field, _palette) in simple_level_tables[dataset.id].items()},
        )
        if dataset.id == "year_capital_by_month":
            _add_sign_metadata(
                records,
                raw,
                {
                    "Валютная переоценка": "__monthly_fx_revaluation_sign",
                    "Расхождение с активами": "__monthly_asset_gap_sign",
                },
            )
    return records


def _grid_column_defs(dataset: DashboardDataset, data: pd.DataFrame, theme: str | None = None, read_only: bool = False) -> list[dict]:
    if dataset.id == "yearly_stats":
        column_defs = []
        for column in data.columns:
            column_def = {"field": column, "cellStyle": _total_row_style(theme)}
            if column == "Доход":
                column_def["cellStyle"] = _merge_total_style(_level_style("__income_level", "green", theme), theme)
            elif column == "Расход":
                column_def["cellStyle"] = _merge_total_style(_level_style("__expense_level", "red", theme), theme)
            elif column == "Сальдо":
                column_def["cellStyle"] = _merge_total_style(_sign_style("__balance_sign", theme), theme)
            elif column == "Процент дохода":
                column_def["cellStyle"] = _merge_total_style(_sign_style("__income_pct_sign", theme), theme)
            column_defs.append(column_def)
        return column_defs

    if dataset.id in {"fx_rates", "year_fx_rates", "month_fx_rates"}:
        widths = {
            "Валюта": {"width": 88, "maxWidth": 100},
            "Курс": {"width": 112, "maxWidth": 128},
            "Обратный курс": {"width": 138, "maxWidth": 158},
            "Источник": {"width": 620, "minWidth": 520},
            "Изменение (%)": {"width": 142, "maxWidth": 160},
        }
        return [
            {
                "field": column,
                "cellStyle": _plain_cell_style(theme),
                **widths.get(column, {"flex": 1}),
            }
            for column in data.columns
        ]

    style_by_dataset = {
        "year_quarter_stats": {
            "Квартал": _total_row_style(theme),
            "Общий доход": _merge_total_style(_level_style("__quarter_income_level", "green", theme), theme),
            "Общий расход": _merge_total_style(_level_style("__quarter_expense_level", "red", theme), theme),
            "Сальдо": _merge_total_style(_sign_style("__quarter_balance_sign", theme), theme),
        },
        "year_income_cost_stats": {
            "Доход": _level_style("__stats_income_level", "green", theme),
            "Расход": _level_style("__stats_expense_level", "red", theme),
        },
        "year_cost_distribution": {
            "Суммарно": _level_style("__cost_sum_level", "red", theme),
            "Среднее": _level_style("__cost_avg_level", "red", theme),
            "Процент": _level_style("__cost_pct_level", "red", theme),
        },
        "month_cost_distribution": {
            "Суммарно": _level_style("__cost_sum_level", "red", theme),
            "Среднее": _level_style("__cost_avg_level", "red", theme),
            "Процент": _level_style("__cost_pct_level", "red", theme),
        },
        "month_transactions": _month_transaction_column_styles(data, theme),
        "month_summary": {
            "Доход": _level_style("__month_summary_income_level", "green", theme),
            "Сбережения": _level_style("__month_summary_savings_level", "green", theme),
            "Расход": _level_style("__month_summary_expense_level", "red", theme),
            "Валютная переоценка": _sign_style("__month_summary_fx_sign", theme),
            "Расхождение с активами": _sign_style("__month_summary_asset_gap_sign", theme),
        },
        "month_assets": _month_asset_column_styles(data, theme),
        "year_income_by_month": {"Доход": _level_style("__monthly_income_level", "green", theme)},
        "year_cost_by_month": {"Расход": _level_style("__monthly_cost_level", "red", theme)},
        "year_capital_by_month": {
            "Капитал": _level_style("__monthly_capital_level", "green", theme),
            "Капитал по активам": _level_style("__monthly_asset_capital_level", "blue", theme),
            "Валютная переоценка": _sign_style("__monthly_fx_revaluation_sign", theme),
            "Расхождение с активами": _sign_style("__monthly_asset_gap_sign", theme),
        },
        "planning_goals": {
            "Цель": _editable_cell_style(theme),
            "Отклонение": _sign_style("__planning_delta_sign", theme),
            "Прогресс (%)": _level_style("__planning_progress_level", "green", theme),
        },
        "planning_fx_scenarios": {
            "Капитал": _level_style("__planning_fx_capital_level", "blue", theme),
            "Изменение капитала": _sign_style("__planning_fx_delta_sign", theme),
        },
        "investment_summary": {
            "Значение": _sign_style("__investment_summary_sign", theme),
        },
        "investment_positions": {
            "Стоимость": _level_style("__investment_value_level", "green", theme),
            "Нереализованный PnL": _sign_style("__investment_unrealized_sign", theme),
            "Реализованный PnL": _sign_style("__investment_realized_sign", theme),
            "Итого PnL": _sign_style("__investment_total_sign", theme),
            "Доля (%)": _level_style("__investment_allocation_level", "blue", theme),
        },
        "investment_allocation_type": {
            "Стоимость": _level_style("__investment_alloc_value_level", "green", theme),
            "Доля (%)": _level_style("__investment_alloc_pct_level", "blue", theme),
        },
        "investment_allocation_currency": {
            "Стоимость": _level_style("__investment_alloc_value_level", "green", theme),
            "Доля (%)": _level_style("__investment_alloc_pct_level", "blue", theme),
        },
    }
    column_styles = style_by_dataset.get(dataset.id, {})
    column_defs = []
    for column in data.columns:
        column_def = {"field": column, "cellStyle": column_styles.get(column, _plain_cell_style(theme))}
        if dataset.id == "planning_goals" and column == "Цель" and not read_only:
            column_def.update({"editable": True, "singleClickEdit": True})
        column_defs.append(column_def)
    return column_defs


def _fill_zero_display_cells(data: pd.DataFrame) -> pd.DataFrame:
    display = data.copy(deep=True)
    for column in display.columns:
        if column == "Дата":
            continue
        display[column] = display[column].replace("", "0").fillna("0")
    return display


def _plain_cell_style(theme: str | None = None) -> dict:
    return {"backgroundColor": "#2b2b2b", "color": "#a9b7c6"} if theme == "dark" else {}


def _editable_cell_style(theme: str | None = None) -> dict:
    if theme == "dark":
        return {
            "backgroundColor": "#333b45",
            "color": "#dcdcdc",
            "border": "1px solid #6897bb",
            "fontWeight": "600",
        }
    return {"backgroundColor": "#eef6ff", "border": "1px solid #9ec5fe", "fontWeight": "600"}


def _add_level_metadata(records: list[dict], raw: pd.DataFrame, field_map: dict[str, str]) -> None:
    max_by_column = {column: _positive_max(raw, column) for column in field_map}
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        for column, field in field_map.items():
            record[field] = _gradient_level(row.get(column), max_by_column[column])


def _add_sign_metadata(records: list[dict], raw: pd.DataFrame, field_map: dict[str, str]) -> None:
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        for column, field in field_map.items():
            record[field] = _value_sign(row.get(column))


def _add_asset_metadata(records: list[dict], raw: pd.DataFrame) -> None:
    value_columns = [column for column in raw.columns if column != "Счет"]
    max_by_column = {column: _positive_max_from_any(raw[column]) for column in value_columns}
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        account = str(row.get("Счет", ""))
        record["__is_asset_total"] = account in {"Всего", "Всего в валюте"}
        record["__is_asset_currency_total"] = account == "Всего в валюте,%"
        row_max = _positive_max_from_any(row[value_columns]) if record["__is_asset_currency_total"] else 0.0
        for column in value_columns:
            if record["__is_asset_total"]:
                record[f"__asset_{column}_level"] = 0
                record[f"__asset_blue_{column}_level"] = 0
            else:
                record[f"__asset_{column}_level"] = _gradient_level(_to_number(row.get(column)), max_by_column[column])
                record[f"__asset_blue_{column}_level"] = _gradient_level(_to_number(row.get(column)), row_max)


def _month_transaction_column_styles(data: pd.DataFrame, theme: str | None = None) -> dict:
    styles = {
        "Доход": _level_style("__month_income_level", "green", theme),
        "Сбережения": _level_style("__month_savings_level", "green", theme),
    }
    expense_columns = [column for column in data.columns if column not in ["Дата", *config.NOT_COST_COLS]]
    styles.update(
        {column: _level_style(f"__month_expense_{index}_level", "red", theme) for index, column in enumerate(expense_columns)}
    )
    return styles


def _month_asset_column_styles(data: pd.DataFrame, theme: str | None = None) -> dict:
    return {
        column: _merge_asset_total_style(
            _merge_row_level_style(
                _level_style(f"__asset_blue_{column}_level", "blue", theme),
                _level_style(f"__asset_{column}_level", "green", theme),
            ),
            theme,
        )
        for column in data.columns
        if column != "Счет"
    } | {"Счет": _merge_asset_currency_total_style(_asset_total_row_style(theme), theme)}


def _asset_total_row_style(theme: str | None = None) -> dict:
    total_style = _total_style(theme)
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_asset_total",
                "style": {
                    **total_style,
                },
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_asset_total_style(style: dict, theme: str | None = None) -> dict:
    total_condition = _asset_total_row_style(theme)["styleConditions"][0]
    return {
        "styleConditions": [total_condition] + style.get("styleConditions", []),
        "defaultStyle": style.get("defaultStyle", {}),
    }


def _asset_currency_total_style(theme: str | None = None) -> dict:
    if theme == "dark":
        base_style = {"backgroundColor": "#3c3f41", "color": "#a9b7c6", "fontWeight": "600"}
    else:
        base_style = {"backgroundColor": "#a9b7c6", "color": "#1e3a8a", "fontWeight": "600"}
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_asset_currency_total",
                "style": base_style,
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_asset_currency_total_style(style: dict, theme: str | None = None) -> dict:
    currency_condition = _asset_currency_total_style(theme)["styleConditions"][0]
    return {
        "styleConditions": style.get("styleConditions", []) + [currency_condition],
        "defaultStyle": style.get("defaultStyle", _plain_cell_style(theme)),
    }


def _merge_row_level_style(primary: dict, secondary: dict) -> dict:
    return {
        "styleConditions": primary.get("styleConditions", []) + secondary.get("styleConditions", []),
        "defaultStyle": primary.get("defaultStyle", secondary.get("defaultStyle", {})),
    }


def _positive_max_from_any(values: pd.Series) -> float:
    numeric = values.map(_to_number).abs().dropna()
    return float(numeric.max()) if not numeric.empty else 0.0


def _to_number(value) -> float:
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value)
    for symbol in ["₽", "$", "€", "₸", "£", "%", " "]:
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.replace(",", ".")
    return pd.to_numeric(cleaned, errors="coerce")


def _positive_max(data: pd.DataFrame, column: str) -> float:
    if column not in data.columns or data.empty:
        return 0.0
    values = pd.to_numeric(data[column], errors="coerce").abs().dropna()
    return float(values.max()) if not values.empty else 0.0


def _gradient_level(value, max_value: float) -> int:
    if max_value <= 0:
        return 0
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric) or numeric <= 0:
        return 0
    ratio = float(numeric) / max_value
    if ratio >= 0.75:
        return 3
    if ratio >= 0.45:
        return 2
    return 1


def _value_sign(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "zero"
    if numeric > 0:
        return "positive"
    if numeric < 0:
        return "negative"
    return "zero"


def _percentage_sign(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "zero"
    if numeric >= 100:
        return "positive"
    return "negative"


def _sign_cell_style(sign: str, theme: str | None = None) -> dict:
    if theme == "dark":
        positive_style = {"backgroundColor": "#31452f", "color": "#b6d7a8"}
        negative_style = {"backgroundColor": "#4a2f2f", "color": "#d99694"}
    else:
        positive_style = {"backgroundColor": "#dff3e3", "color": "#1f5130"}
        negative_style = {"backgroundColor": "#f8dddd", "color": "#6b2626"}
    if sign == "positive":
        return {**positive_style, "fontWeight": "600"}
    if sign == "negative":
        return {**negative_style, "fontWeight": "600"}
    return {}


def _sign_style(field: str, theme: str | None = None) -> dict:
    if theme == "dark":
        positive_style = {"backgroundColor": "#31452f", "color": "#b6d7a8"}
        negative_style = {"backgroundColor": "#4a2f2f", "color": "#d99694"}
    else:
        positive_style = {"backgroundColor": "#dff3e3", "color": "#1f5130"}
        negative_style = {"backgroundColor": "#f8dddd", "color": "#6b2626"}
    return {
        "styleConditions": [
            {
                "condition": f"params.data.{field} == 'positive'",
                "style": positive_style,
            },
            {
                "condition": f"params.data.{field} == 'negative'",
                "style": negative_style,
            },
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _total_style(theme: str | None = None) -> dict:
    if theme == "dark":
        return {"backgroundColor": "#555555", "color": "#dcdcdc", "fontWeight": "600"}
    return {"backgroundColor": "rgba(108, 117, 125, 0.16)", "color": "#2f3742", "fontWeight": "600"}


def _total_row_style(theme: str | None = None) -> dict:
    total_style = _total_style(theme)
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_total",
                "style": {
                    **total_style,
                },
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_total_style(style: dict, theme: str | None = None) -> dict:
    total_condition = _total_row_style(theme)["styleConditions"][0]
    return {
        "styleConditions": [total_condition] + style.get("styleConditions", []),
        "defaultStyle": style.get("defaultStyle", {}),
    }


def _level_palette(palette: str, theme: str | None = None) -> tuple[dict[int, str], str]:
    if theme == "dark" and palette == "green":
        colors = {1: "#2f3d2f", 2: "#3d5a3a", 3: "#4f714b"}
        text_color = "#e2f4da"
    elif theme == "dark" and palette == "blue":
        colors = {1: "#2f3d4a", 2: "#38546b", 3: "#4a6f8a"}
        text_color = "#e6f2ff"
    elif theme == "dark":
        colors = {1: "#3f2d2d", 2: "#5a3838", 3: "#704444"}
        text_color = "#f1b8b6"
    elif palette == "green":
        colors = {1: "#edf8ef", 2: "#d7efd9", 3: "#bde5c0"}
        text_color = "#214d2c"
    elif palette == "blue":
        colors = {1: "#eff6ff", 2: "#a9b7c6", 3: "#a9b7c6"}
        text_color = "#1e3a8a"
    else:
        colors = {1: "#fff0ed", 2: "#f9d8d2", 3: "#f1c0b8"}
        text_color = "#63312c"

    return colors, text_color


def _level_cell_style(level, palette: str, theme: str | None = None) -> dict:
    numeric_level = pd.to_numeric(level, errors="coerce")
    if pd.isna(numeric_level) or int(numeric_level) <= 0:
        return {}
    colors, text_color = _level_palette(palette, theme)
    level_key = min(max(int(numeric_level), 1), 3)
    return {"backgroundColor": colors[level_key], "color": text_color, "fontWeight": "600"}


def _level_style(field: str, palette: str, theme: str | None = None) -> dict:
    colors, text_color = _level_palette(palette, theme)

    return {
        "styleConditions": [
            {
                "condition": f"params.data.{field} == 3",
                "style": {"backgroundColor": colors[3], "color": text_color},
            },
            {
                "condition": f"params.data.{field} == 2",
                "style": {"backgroundColor": colors[2], "color": text_color},
            },
            {
                "condition": f"params.data.{field} == 1",
                "style": {"backgroundColor": colors[1], "color": text_color},
            },
        ],
        "defaultStyle": _plain_cell_style(theme),
    }
