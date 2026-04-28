import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src import config


TRANSACTION_FILE_RE = re.compile(r"^(?P<year>\d{4})_(?P<month>\d{2})_?\.csv$")
ASSET_FILE_RE = re.compile(r"^(?P<year>\d{4})_(?P<month>\d{2})\.csv$")


@dataclass(frozen=True)
class ValidationIssue:
    path: Path
    message: str

    def __str__(self) -> str:
        try:
            display_path = self.path.relative_to(config.PROJECT_PATH)
        except ValueError:
            display_path = self.path
        return f"{display_path}: {self.message}"


class DataValidationError(Exception):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__(_format_issues(issues))


def validate_all_data(raise_on_error: bool = True) -> list[ValidationIssue]:
    """
    Validate source CSV files before reports are generated.
    """
    issues = []
    issues.extend(validate_transactions())
    issues.extend(validate_assets())
    issues.extend(validate_investments())

    if issues and raise_on_error:
        raise DataValidationError(issues)

    return issues


def validate_transactions() -> list[ValidationIssue]:
    issues = []
    root = Path(config.TRANSACTIONS_INFO_PATH)
    if not root.exists():
        return [ValidationIssue(root, "transactions folder does not exist")]

    for csv_path in sorted(root.glob("*/*.csv")):
        issues.extend(_validate_transaction_file(csv_path))

    return issues


def validate_assets() -> list[ValidationIssue]:
    issues = []
    root = Path(config.ASSETS_INFO_PATH)
    if not root.exists():
        return [ValidationIssue(root, "assets folder does not exist")]

    for csv_path in sorted(root.glob("*/*.csv")):
        issues.extend(_validate_asset_file(csv_path))

    return issues


def validate_investments() -> list[ValidationIssue]:
    csv_path = Path(config.INVESTMENTS_PATH)
    if not csv_path.exists():
        return [ValidationIssue(csv_path, "investments file does not exist")]

    issues = []
    try:
        data = pd.read_csv(csv_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        return [ValidationIssue(csv_path, f"cannot read investments CSV: {exc}")]

    required_columns = {"Тип_транзакции", "Актив", "Тикер", "Количество", "Дата", "Цена"}
    issues.extend(_missing_columns(csv_path, data, required_columns))
    if issues:
        return issues

    dates = pd.to_datetime(data["Дата"], dayfirst=True, errors="coerce")
    for row_number, is_bad in enumerate(dates.isna(), start=2):
        if is_bad:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: invalid investment date"))

    for row_number, value in enumerate(data["Количество"], start=2):
        if not _is_number(value):
            issues.append(ValidationIssue(csv_path, f"row {row_number}: invalid investment quantity {value!r}"))

    for row_number, value in enumerate(data["Цена"], start=2):
        parsed = _parse_money_cell(value, expected_parts=2)
        if parsed is None:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: invalid investment price {value!r}"))
            continue
        _, currency = parsed
        if currency not in config.UNIQUE_TICKERS:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: unsupported currency {currency!r}"))

    return issues


def _validate_transaction_file(csv_path: Path) -> list[ValidationIssue]:
    issues = []
    match = TRANSACTION_FILE_RE.match(csv_path.name)
    if not match:
        issues.append(ValidationIssue(csv_path, "transaction filename should look like YYYY_MM.csv or YYYY_MM_.csv"))

    if match and csv_path.parent.name != match.group("year"):
        issues.append(ValidationIssue(csv_path, "parent folder year does not match filename year"))

    try:
        data = pd.read_csv(csv_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        return [ValidationIssue(csv_path, f"cannot read transaction CSV: {exc}")]

    issues.extend(_missing_columns(csv_path, data, {"Дата"}))
    if "Дата" not in data.columns:
        return issues

    dates = pd.to_datetime(data["Дата"], dayfirst=True, errors="coerce")
    for row_number, is_bad in enumerate(dates.isna(), start=2):
        if is_bad:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: invalid transaction date"))

    if match and not dates.isna().all():
        expected_year = int(match.group("year"))
        expected_month = int(match.group("month"))
        wrong_dates = dates.dropna()[
            (dates.dropna().dt.year != expected_year) | (dates.dropna().dt.month != expected_month)
        ]
        if not wrong_dates.empty:
            issues.append(ValidationIssue(csv_path, "contains dates outside filename year/month"))

    money_columns = [column for column in data.columns if column != "Дата"]
    for column in money_columns:
        for row_number, value in enumerate(data[column], start=2):
            for part in _split_transaction_cell(value):
                parsed = _parse_money_cell(part, expected_parts=3)
                if parsed is None:
                    issues.append(ValidationIssue(csv_path, f"row {row_number}, column {column!r}: invalid value {part!r}"))
                    continue
                currency = parsed[1]
                if currency not in config.UNIQUE_TICKERS:
                    issues.append(
                        ValidationIssue(csv_path, f"row {row_number}, column {column!r}: unsupported currency {currency!r}")
                    )

    return issues


def _validate_asset_file(csv_path: Path) -> list[ValidationIssue]:
    issues = []
    match = ASSET_FILE_RE.match(csv_path.name)
    if not match:
        issues.append(ValidationIssue(csv_path, "asset filename should look like YYYY_MM.csv"))

    if match and csv_path.parent.name != match.group("year"):
        issues.append(ValidationIssue(csv_path, "parent folder year does not match filename year"))

    try:
        data = pd.read_csv(csv_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    except Exception as exc:
        return [ValidationIssue(csv_path, f"cannot read asset CSV: {exc}")]

    issues.extend(_missing_columns(csv_path, data, {"Счет", "Сумма"}))
    if "Сумма" not in data.columns:
        return issues

    for row_number, value in enumerate(data["Сумма"], start=2):
        parsed = _parse_money_cell(value, expected_parts=2)
        if parsed is None:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: invalid asset amount {value!r}"))
            continue
        _, currency = parsed
        if currency not in config.UNIQUE_TICKERS:
            issues.append(ValidationIssue(csv_path, f"row {row_number}: unsupported currency {currency!r}"))

    return issues


def _missing_columns(path: Path, data: pd.DataFrame, required_columns: set[str]) -> list[ValidationIssue]:
    missing = sorted(required_columns - set(data.columns))
    return [ValidationIssue(path, f"missing required column {column!r}") for column in missing]


def _split_transaction_cell(value: str) -> list[str]:
    normalized = _normalize_money_text(value)
    if normalized == "":
        return ["0"]
    return [part.strip() for part in normalized.split("#")]


def _parse_money_cell(value: str, expected_parts: int):
    normalized = _normalize_money_text(value)
    parts = [part.strip() for part in normalized.split("|")]

    if len(parts) == 1:
        return (float(parts[0]), "RUB") if _is_number(parts[0]) else None

    if len(parts) != expected_parts:
        return None

    amount = parts[0]
    currency = parts[1].upper()
    if not _is_number(amount) or not currency:
        return None

    if expected_parts == 2:
        return float(amount), currency
    return float(amount), currency, parts[2]


def _normalize_money_text(value: str) -> str:
    return (
        str(value)
        .strip()
        .replace(",", ".")
        .replace("\\xa0", "")
        .replace("\xa0", "")
        .replace(" ₽", "")
    )


def _is_number(value: str) -> bool:
    try:
        float(_normalize_money_text(value))
    except (TypeError, ValueError):
        return False
    return True


def _format_issues(issues: list[ValidationIssue]) -> str:
    preview = "\n".join(str(issue) for issue in issues[:20])
    if len(issues) > 20:
        preview += f"\n... and {len(issues) - 20} more issue(s)"
    return f"Data validation failed with {len(issues)} issue(s):\n{preview}"
