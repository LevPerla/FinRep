"""Conservative source labels for historical Income transactions."""

import re
import unicodedata

import pandas as pd


SALARY = re.compile(r"(?<!\w)(?:зарплат\w*|заработн\w*\s+плат\w*|з\s*/\s*п|salary|payroll)(?!\w)")
INTEREST = re.compile(r"(?<!\w)(?:процент\w*|interest)(?!\w)")
DEPOSIT = re.compile(r"(?<!\w)(?:депозит\w*|вклад\w*|deposit\w*)(?!\w)")
NEGATED_SOURCE = re.compile(r"(?<!\w)не\s+(?:зарплат\w*|заработн\w*\s+плат\w*|salary|payroll|процент\w*|interest)(?!\w)")
EXACT_ALIASES = {
    "зараплата": "salary",
    "премия": "salary",
    "отпускные": "salary",
    "выплаты за отпуск": "salary",
    "ретеншн бонус": "salary",
    "проценты на вклда": "deposit_interest",
    "проценты": "deposit_interest",
    "процент": "deposit_interest",
    "депозит": "deposit_interest",
    "вклад": "deposit_interest",
    "с депозита": "deposit_interest",
}


def classify_income_comment(comment) -> str:
    """Return a source or the reason why the historical source is unknown."""
    if pd.isna(comment):
        return "unknown_empty"
    normalized = " ".join(unicodedata.normalize("NFKC", str(comment)).casefold().replace("ё", "е").split())
    if not normalized:
        return "unknown_empty"
    if NEGATED_SOURCE.search(normalized):
        return "unknown_text"
    if normalized in EXACT_ALIASES:
        return EXACT_ALIASES[normalized]
    salary = bool(SALARY.search(normalized))
    deposit_interest = bool(INTEREST.search(normalized) and DEPOSIT.search(normalized))
    if salary and deposit_interest:
        return "conflict"
    if salary:
        return "salary"
    if deposit_interest:
        return "deposit_interest"
    return "unknown_text"


def classify_income_transactions(transactions: pd.DataFrame) -> pd.DataFrame:
    """Classify nonzero Income rows without modifying or aggregating them."""
    income = transactions.loc[
        transactions["Категория"].eq("Доход") & transactions["Значение"].ne(0)
    ].copy()
    income["source_reason"] = income["Комментарий"].map(classify_income_comment)
    income["income_source"] = income["source_reason"].where(
        income["source_reason"].isin(("salary", "deposit_interest")), "unknown"
    )
    return income
