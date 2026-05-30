import pandas as pd
import plotly.graph_objects as go

from src import config
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.crypto import read_crypto_balances, read_crypto_refresh_status, read_crypto_wallets, validate_crypto_wallets
from src.data.investment_calculations import calculate_portfolio
from src.data.investments import latest_cached_prices


def build_investment_dashboard_data(
    currency: str,
    fx_network_enabled: bool = True,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    portfolio = calculate_portfolio(currency)
    summary = portfolio["summary"]
    positions = portfolio["positions"]
    allocation_by_type = portfolio["allocation_by_type"]
    allocation_by_currency = portfolio["allocation_by_currency"]
    crypto_wallets = _crypto_wallet_status()

    return {
        "investment_summary": DashboardDataset(
            id="investment_summary",
            title="Сводка портфеля",
            dataframe=summary,
            display_dataframe=_format_summary(summary, currency),
        ),
        "investment_positions": DashboardDataset(
            id="investment_positions",
            title="Позиции",
            dataframe=positions,
            display_dataframe=_format_positions(positions, currency),
        ),
        "investment_allocation_type": DashboardDataset(
            id="investment_allocation_type",
            title="Аллокация по типам активов",
            dataframe=allocation_by_type,
            display_dataframe=_format_allocation(allocation_by_type, "asset_type", currency),
            figure=_allocation_figure(allocation_by_type, "asset_type"),
        ),
        "investment_allocation_currency": DashboardDataset(
            id="investment_allocation_currency",
            title="Аллокация по валютам",
            dataframe=allocation_by_currency,
            display_dataframe=_format_allocation(allocation_by_currency, "currency", currency),
            figure=_allocation_figure(allocation_by_currency, "currency"),
        ),
        "crypto_wallets": DashboardDataset(
            id="crypto_wallets",
            title="Crypto кошельки",
            dataframe=crypto_wallets,
            display_dataframe=crypto_wallets.copy(deep=True),
        ),
    }


def _format_summary(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    metric_names = {
        "Portfolio value": "Стоимость портфеля",
        "Unrealized PnL": "Нереализованный PnL",
        "Realized PnL": "Реализованный PnL",
        "Total PnL": "Итого PnL",
    }
    display["metric"] = display["metric"].map(metric_names).fillna(display["metric"])
    display = display.rename(columns={"metric": "Показатель", "value": "Значение", "currency": "Валюта"})
    display["Значение"] = _format_money(display["Значение"], currency)
    return display


def _format_positions(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    if data.empty:
        return data.copy(deep=True)
    display = data.copy(deep=True).rename(
        columns={
            "ticker": "Тикер",
            "asset_type": "Тип",
            "quantity": "Количество",
            "average_cost": "Средняя цена",
            "latest_price": "Цена",
            "currency": "Валюта цены",
            "cost_basis": "Себестоимость",
            "market_value": "Стоимость",
            "unrealized_pnl": "Нереализованный PnL",
            "realized_pnl": "Реализованный PnL",
            "total_pnl": "Итого PnL",
            "allocation": "Доля (%)",
            "price_date": "Дата цены",
            "price_source": "Источник цены",
            "account": "Аккаунт",
            "chain": "Сеть",
        }
    )
    display["Количество"] = pd.to_numeric(display["Количество"], errors="coerce").map(lambda value: f"{value:,.6f}".replace(",", " ").rstrip("0").rstrip("."))
    for column in ["Средняя цена", "Цена"]:
        display[column] = pd.to_numeric(display[column], errors="coerce").map(lambda value: f"{value:,.2f}".replace(",", " "))
    for column in ["Себестоимость", "Стоимость", "Нереализованный PnL", "Реализованный PnL", "Итого PnL"]:
        display[column] = _format_money(display[column], currency)
    display["Доля (%)"] = pd.to_numeric(display["Доля (%)"], errors="coerce").map("{:,.2f}%".format)
    columns = [
        "Тикер",
        "Тип",
        "Аккаунт",
        "Сеть",
        "Количество",
        "Средняя цена",
        "Цена",
        "Валюта цены",
        "Стоимость",
        "Нереализованный PnL",
        "Реализованный PnL",
        "Итого PnL",
        "Доля (%)",
        "Дата цены",
        "Источник цены",
    ]
    return display[[column for column in columns if column in display.columns]]


def _format_allocation(data: pd.DataFrame, dimension: str, currency: str) -> pd.DataFrame:
    if data.empty:
        return data.copy(deep=True)
    title = "Тип" if dimension == "asset_type" else "Валюта"
    display = data.copy(deep=True).rename(columns={dimension: title, "market_value": "Стоимость", "allocation": "Доля (%)"})
    display["Стоимость"] = _format_money(display["Стоимость"], currency)
    display["Доля (%)"] = pd.to_numeric(display["Доля (%)"], errors="coerce").map("{:,.2f}%".format)
    return display


def _allocation_figure(data: pd.DataFrame, dimension: str) -> go.Figure:
    fig = go.Figure()
    if not data.empty:
        fig.add_trace(
            go.Pie(
                labels=data[dimension],
                values=data["market_value"],
                hole=0.42,
                textinfo="label+percent",
            )
        )
    _apply_dashboard_chart_layout(fig, "Аллокация")
    fig.update_layout(showlegend=True)
    return fig


def _format_money(values: pd.Series, currency: str) -> pd.Series:
    symbol = config.UNIQUE_TICKERS[currency]
    return pd.to_numeric(values, errors="coerce").fillna(0.0).map(lambda value: f"{value:,.2f}".replace(",", " ") + symbol)


def _crypto_wallet_status() -> pd.DataFrame:
    wallets = read_crypto_wallets()
    if wallets.empty:
        return pd.DataFrame(columns=["Сеть", "Актив", "Адрес", "Последний баланс", "Цена", "Статус"])

    issues_by_row: dict[int, list[str]] = {}
    for issue in validate_crypto_wallets():
        if issue.row_number is not None:
            issues_by_row.setdefault(issue.row_number, []).append(issue.message)

    balances = read_crypto_balances()
    if not balances.empty:
        balances = balances.copy(deep=True)
        balances["fetched_sort"] = pd.to_datetime(balances["fetched_at"], errors="coerce")
        balances = balances.sort_values(["account", "chain", "asset", "address", "fetched_sort"], kind="mergesort")
        balances = balances.groupby(["account", "chain", "asset", "address"], as_index=False).tail(1)
        balance_by_key = {
            (row["account"], row["chain"], row["asset"], row["address"]): row
            for _, row in balances.iterrows()
        }
    else:
        balance_by_key = {}

    prices = latest_cached_prices(seed_missing=False)
    price_by_ticker = prices.set_index("ticker").to_dict("index") if not prices.empty else {}
    refresh_status = read_crypto_refresh_status()
    if not refresh_status.empty:
        refresh_status["row_number_int"] = pd.to_numeric(refresh_status["row_number"], errors="coerce")
        refresh_status["fetched_sort"] = pd.to_datetime(refresh_status["fetched_at"], errors="coerce")
        refresh_status = refresh_status.sort_values(["row_number_int", "fetched_sort"], kind="mergesort")
        status_by_row = {int(row["row_number_int"]): row for _, row in refresh_status.dropna(subset=["row_number_int"]).groupby("row_number_int", as_index=False).tail(1).iterrows()}
    else:
        status_by_row = {}
    rows = []
    for index, wallet in wallets.iterrows():
        row_number = int(index) + 2
        key = (wallet["account"], wallet["chain"], wallet["asset"], wallet["address"])
        balance = balance_by_key.get(key)
        price = price_by_ticker.get(wallet["asset"], {})
        enabled = str(wallet["enabled"]).strip().lower() not in {"0", "false", "no", "off"}
        status_parts = []
        if not enabled:
            status_parts.append("disabled")
        status_parts.extend(issues_by_row.get(row_number, []))
        if enabled and balance is None:
            status_parts.append("нет balance cache")
        if enabled and wallet["asset"] not in price_by_ticker:
            status_parts.append("нет price cache")
        last_status = status_by_row.get(row_number)
        if last_status is not None:
            status_parts.append(f"last refresh {last_status.get('status')}: {last_status.get('message')}")
        if enabled and not status_parts:
            status_parts.append("ok")
        rows.append(
            {
                "Строка": row_number,
                "Аккаунт": wallet["account"],
                "Сеть": wallet["chain"],
                "Актив": wallet["asset"],
                "Включен": "yes" if enabled else "no",
                "Адрес": wallet["address"],
                "Последний баланс": "" if balance is None else str(balance.get("balance", "")),
                "Цена": "" if not price else f"{price.get('price', '')} {price.get('currency', '')} ({price.get('source', '')})",
                "Статус": "; ".join(status_parts),
            }
        )
    data = pd.DataFrame(rows)
    return data.drop(columns=["Строка", "Аккаунт", "Включен"], errors="ignore")
