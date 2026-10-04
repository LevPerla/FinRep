from __future__ import annotations

import calendar
from datetime import date


STALE_DAYS_BY_ASSET_TYPE = {
    "cash_account": 35,
    "deposit": 35,
    "bond": 7,
    "equity": 7,
    "fund": 7,
    "crypto": 7,
    "real_estate": 365,
}
DEFAULT_STALE_DAYS = 35


def evaluate_asset_freshness(
        accounts: list[dict], *, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    evaluated = []
    for account in accounts:
        if not bool(account.get("active", 1)):
            evaluated.append({
                **account,
                "freshness_status": "archived",
                "valuation_age_days": None,
                "stale_threshold_days": None,
            })
            continue
        threshold_days = STALE_DAYS_BY_ASSET_TYPE.get(
            account.get("asset_type_id"), DEFAULT_STALE_DAYS)
        period = account.get("last_period")
        age_days = None
        if period:
            year, month = (int(value) for value in str(period).split("-"))
            snapshot_date = date(year, month, calendar.monthrange(year, month)[1])
            if snapshot_date > as_of and (year, month) == (as_of.year, as_of.month):
                snapshot_date = as_of
            age_days = (as_of - snapshot_date).days
            status = "stale" if age_days > threshold_days else "fresh"
        else:
            status = "missing"
        evaluated.append({
            **account,
            "freshness_status": status,
            "valuation_age_days": age_days,
            "stale_threshold_days": threshold_days,
        })

    included = [
        row for row in evaluated
        if bool(row.get("active", 1)) and bool(row.get("include_in_capital", 1))
    ]
    stale = [row for row in included if row["freshness_status"] == "stale"]
    missing = [row for row in included if row["freshness_status"] == "missing"]
    return {
        "as_of": as_of.isoformat(),
        "accounts": evaluated,
        "included_count": len(included),
        "stale_count": len(stale),
        "missing_count": len(missing),
        "stale_accounts": [row["name"] for row in stale],
        "missing_accounts": [row["name"] for row in missing],
        "has_warning": bool(stale or missing),
    }


def freshness_label(account: dict, *, locale: str = "ru") -> str:
    status = account["freshness_status"]
    age_days = account["valuation_age_days"]
    threshold = account["stale_threshold_days"]
    if locale == "en":
        if status == "archived":
            return f"Archived · {account.get('closed_period', '')}".rstrip(" ·")
        if status == "missing":
            return "Valuation date unknown"
        label = "Stale" if status == "stale" else "Current"
        return f"{label} · {age_days} d (limit {threshold})"
    if status == "archived":
        return f"В архиве · {account.get('closed_period', '')}".rstrip(" ·")
    if status == "missing":
        return "Дата оценки неизвестна"
    label = "Устарело" if status == "stale" else "Актуально"
    return f"{label} · {age_days} дн. (порог {threshold})"
