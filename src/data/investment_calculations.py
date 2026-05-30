from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from src.data.crypto import calculate_crypto_positions
from src.data.get_finance import get_actual_fx_rate, get_fallback_rate
from src.data.investments import latest_cached_prices, read_investment_transactions


@dataclass
class _Lot:
    quantity: float
    unit_cost: float


def calculate_portfolio(currency: str) -> dict[str, pd.DataFrame]:
    currency = str(currency).upper()
    transactions = read_investment_transactions()
    positions = _combined_positions(calculate_positions(transactions, currency), calculate_crypto_positions(currency))
    realized = realized_pnl_by_ticker(transactions, currency)
    summary = portfolio_summary(positions, currency, realized)
    allocation_by_type = portfolio_allocation(positions, "asset_type")
    allocation_by_currency = portfolio_allocation(positions, "currency")
    return {
        "positions": positions,
        "summary": summary,
        "allocation_by_type": allocation_by_type,
        "allocation_by_currency": allocation_by_currency,
        "realized_pnl": realized,
    }


def _combined_positions(investment_positions: pd.DataFrame, crypto_positions: pd.DataFrame) -> pd.DataFrame:
    positions = pd.concat([investment_positions, crypto_positions], ignore_index=True, sort=False).fillna("")
    if positions.empty:
        return _empty_positions()
    for column in ["quantity", "average_cost", "latest_price", "cost_basis", "market_value", "unrealized_pnl", "realized_pnl", "total_pnl", "sold_quantity"]:
        if column in positions.columns:
            positions[column] = pd.to_numeric(positions[column], errors="coerce").fillna(0.0)
    total_value = positions["market_value"].sum()
    positions["allocation"] = positions["market_value"] / total_value * 100 if total_value else 0.0
    return positions.sort_values("market_value", ascending=False, kind="mergesort").reset_index(drop=True)


def calculate_positions(transactions: pd.DataFrame | None = None, currency: str = "RUB") -> pd.DataFrame:
    data = read_investment_transactions() if transactions is None else transactions.copy(deep=True)
    if data.empty:
        return _empty_positions()

    data = _normalized_transactions(data)
    latest_prices = latest_cached_prices(data)
    price_by_ticker = latest_prices.set_index("ticker").to_dict("index") if not latest_prices.empty else {}
    rows = []

    for ticker, ticker_rows in data.sort_values("date", kind="mergesort").groupby("ticker", sort=True):
        lots: list[_Lot] = []
        realized_native = 0.0
        sold_quantity = 0.0
        asset_type = ""
        native_currency = ""

        for _, row in ticker_rows.iterrows():
            asset_type = row["asset_type"]
            native_currency = row["currency"]
            quantity = float(row["quantity"])
            price = float(row["price"])
            fee = float(row["fee"])
            if row["operation"] == "buy":
                lots.append(_Lot(quantity=quantity, unit_cost=price + fee / quantity if quantity else price))
                continue

            remaining_to_sell = quantity
            proceeds = quantity * price - fee
            cost_basis = 0.0
            while remaining_to_sell > 0 and lots:
                lot = lots[0]
                consumed = min(lot.quantity, remaining_to_sell)
                cost_basis += consumed * lot.unit_cost
                lot.quantity -= consumed
                remaining_to_sell -= consumed
                if lot.quantity <= 1e-12:
                    lots.pop(0)
            realized_native += proceeds - cost_basis
            sold_quantity += quantity

        quantity = sum(lot.quantity for lot in lots)
        if quantity <= 1e-12:
            continue

        cost_basis = sum(lot.quantity * lot.unit_cost for lot in lots)
        average_cost = cost_basis / quantity if quantity else 0.0
        price_info = price_by_ticker.get(ticker, {})
        latest_price = _to_float(price_info.get("price"), average_cost)
        price_currency = str(price_info.get("currency") or native_currency).upper()
        native_rate = _conversion_rate(price_currency, currency)
        market_value_native = quantity * latest_price
        unrealized_native = market_value_native - cost_basis

        rows.append(
            {
                "ticker": ticker,
                "asset_type": asset_type,
                "quantity": quantity,
                "average_cost": average_cost,
                "latest_price": latest_price,
                "currency": price_currency,
                "cost_basis": cost_basis,
                "market_value": market_value_native * native_rate,
                "unrealized_pnl": unrealized_native * native_rate,
                "realized_pnl": realized_native * native_rate,
                "total_pnl": (realized_native + unrealized_native) * native_rate,
                "allocation": 0.0,
                "price_date": price_info.get("date", ""),
                "price_source": price_info.get("source", ""),
                "sold_quantity": sold_quantity,
            }
        )

    positions = pd.DataFrame(rows)
    if positions.empty:
        return _empty_positions()
    total_value = positions["market_value"].sum()
    positions["allocation"] = positions["market_value"] / total_value * 100 if total_value else 0.0
    return positions.sort_values("market_value", ascending=False, kind="mergesort").reset_index(drop=True)


def realized_pnl_by_ticker(transactions: pd.DataFrame | None = None, currency: str = "RUB") -> pd.DataFrame:
    data = read_investment_transactions() if transactions is None else transactions.copy(deep=True)
    if data.empty:
        return pd.DataFrame(columns=["ticker", "realized_pnl", "currency"])
    data = _normalized_transactions(data)
    rows = []
    for ticker, ticker_rows in data.groupby("ticker", sort=True):
        realized_native, native_currency = _calculate_realized_native(ticker_rows)
        rows.append(
            {
                "ticker": ticker,
                "realized_pnl": realized_native * _conversion_rate(native_currency, currency),
                "currency": currency,
            }
        )
    return pd.DataFrame(rows).sort_values("ticker", kind="mergesort").reset_index(drop=True)


def portfolio_summary(positions: pd.DataFrame, currency: str, realized: pd.DataFrame | None = None) -> pd.DataFrame:
    realized_total = 0.0 if realized is None or realized.empty else float(realized["realized_pnl"].sum())
    if positions.empty:
        return pd.DataFrame(
            [
                {"metric": "Portfolio value", "value": 0.0, "currency": currency},
                {"metric": "Unrealized PnL", "value": 0.0, "currency": currency},
                {"metric": "Realized PnL", "value": realized_total, "currency": currency},
                {"metric": "Total PnL", "value": realized_total, "currency": currency},
            ]
        )
    unrealized_total = float(positions["unrealized_pnl"].sum())
    return pd.DataFrame(
        [
            {"metric": "Portfolio value", "value": positions["market_value"].sum(), "currency": currency},
            {"metric": "Unrealized PnL", "value": unrealized_total, "currency": currency},
            {"metric": "Realized PnL", "value": realized_total, "currency": currency},
            {"metric": "Total PnL", "value": realized_total + unrealized_total, "currency": currency},
        ]
    )


def portfolio_allocation(positions: pd.DataFrame, column: str) -> pd.DataFrame:
    if positions.empty or column not in positions.columns:
        return pd.DataFrame(columns=[column, "market_value", "allocation"])
    grouped = positions.groupby(column, as_index=False)["market_value"].sum().sort_values("market_value", ascending=False)
    total = grouped["market_value"].sum()
    grouped["allocation"] = grouped["market_value"] / total * 100 if total else 0.0
    return grouped.reset_index(drop=True)


def current_investment_value(currency: str) -> float:
    positions = calculate_portfolio(currency)["positions"]
    if positions.empty:
        return 0.0
    return float(positions["market_value"].sum())


def _normalized_transactions(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    normalized["date"] = pd.to_datetime(normalized["date"], errors="coerce")
    for column in ["quantity", "price", "fee"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0)
    return normalized.dropna(subset=["date"]).sort_values(["date", "ticker"], kind="mergesort")


def _calculate_realized_native(ticker_rows: pd.DataFrame) -> tuple[float, str]:
    lots: list[_Lot] = []
    realized_native = 0.0
    native_currency = ""

    for _, row in ticker_rows.sort_values("date", kind="mergesort").iterrows():
        native_currency = row["currency"]
        quantity = float(row["quantity"])
        price = float(row["price"])
        fee = float(row["fee"])
        if row["operation"] == "buy":
            lots.append(_Lot(quantity=quantity, unit_cost=price + fee / quantity if quantity else price))
            continue

        remaining_to_sell = quantity
        proceeds = quantity * price - fee
        cost_basis = 0.0
        while remaining_to_sell > 0 and lots:
            lot = lots[0]
            consumed = min(lot.quantity, remaining_to_sell)
            cost_basis += consumed * lot.unit_cost
            lot.quantity -= consumed
            remaining_to_sell -= consumed
            if lot.quantity <= 1e-12:
                lots.pop(0)
        realized_native += proceeds - cost_basis

    return realized_native, native_currency


def _conversion_rate(from_currency: str, to_currency: str) -> float:
    from_currency = str(from_currency).upper()
    to_currency = str(to_currency).upper()
    if from_currency == to_currency:
        return 1.0
    rate = get_actual_fx_rate(from_currency, to_currency)
    if rate is None:
        rate = get_fallback_rate(from_currency, to_currency)
    return float(rate) if rate is not None else 1.0


def _to_float(value, default: float = 0.0) -> float:
    parsed = pd.to_numeric(value, errors="coerce")
    return float(parsed) if pd.notna(parsed) else float(default)


def _empty_positions() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "asset_type",
            "quantity",
            "average_cost",
            "latest_price",
            "currency",
            "cost_basis",
            "market_value",
            "unrealized_pnl",
            "realized_pnl",
            "total_pnl",
            "allocation",
            "price_date",
            "price_source",
            "sold_quantity",
        ]
    )
