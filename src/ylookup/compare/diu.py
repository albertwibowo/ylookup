"""Normalize and score DIU sheets against a target."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

REQUIRED_FIELDS = [
    "Legal Entity",
    "Legal Entity Domain",
    "Deal",
    "Position",
    "Batch Index",
    "Batch Type",
    "JE Index",
    "Trans Index",
    "GL Date",
    "Effective Date",
    "Transaction Currency",
    "Allocation Rule",
    "Transaction Type",
    "Account",
    "Is Debit",
    "Amount (Local)",
    "Amount (LE)",
    "Transaction Comments",
    "Batch Comments",
    "Bank Account",
    "Investor",
    "Investor Domain",
    "Vehicle",
    "Investor Amount (LE)",
    "Investor Amount (Local)",
    "Transaction GL Reference",
]

NUMERIC_FIELDS = {
    "Amount (Local)",
    "Amount (LE)",
    "Investor Amount (LE)",
    "Investor Amount (Local)",
    "Batch Index",
    "JE Index",
    "Trans Index",
    "Transaction GL Reference",
}

DATE_FIELDS = {"GL Date", "Effective Date"}


def _blank_eq(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _norm_text(value: Any) -> str | None:
    if _blank_eq(value):
        return None
    return str(value).strip()


def _norm_number(value: Any) -> float | None:
    if _blank_eq(value):
        return None
    if isinstance(value, bool):
        return float(int(value))
    return float(value)


def _norm_date(value: Any) -> date | None:
    if _blank_eq(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # Excel serial or string — try parse ISO-ish
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unparseable date: {value!r}")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in REQUIRED_FIELDS:
        raw = row.get(key)
        if key in NUMERIC_FIELDS:
            out[key] = _norm_number(raw)
        elif key in DATE_FIELDS:
            out[key] = _norm_date(raw)
        else:
            out[key] = _norm_text(raw)
    return out


def row_key(row: dict[str, Any]) -> tuple:
    n = normalize_row(row)
    return (n.get("JE Index"), n.get("Trans Index"), n.get("Transaction GL Reference"))


@dataclass
class FieldDiff:
    row_index: int
    field: str
    expected: Any
    actual: Any


@dataclass
class CompareResult:
    score: float
    matched_rows: int
    expected_rows: int
    actual_rows: int
    diffs: list[FieldDiff] = field(default_factory=list)
    passed: bool = False
    summary: str = ""

    def failing_checks(self, limit: int = 20) -> list[str]:
        lines = []
        for d in self.diffs[:limit]:
            lines.append(
                f"row {d.row_index} {d.field}: expected={d.expected!r} actual={d.actual!r}"
            )
        if len(self.diffs) > limit:
            lines.append(f"... and {len(self.diffs) - limit} more")
        return lines


def values_equal(field: str, expected: Any, actual: Any) -> bool:
    if field in DATE_FIELDS:
        # Blank-equivalent for unpaid dates
        if expected is None and actual is None:
            return True
        return expected == actual
    if field in NUMERIC_FIELDS:
        if expected is None and actual is None:
            return True
        if expected is None or actual is None:
            # Investor amounts on cash rows are blank in both; treat None==None only
            return False
        return abs(float(expected) - float(actual)) < 1e-6
    # text / flags
    return expected == actual


def compare_diu(
    actual_rows: list[dict[str, Any]],
    expected_rows: list[dict[str, Any]],
) -> CompareResult:
    """Score DIU actual vs target on required fields."""
    exp = [normalize_row(r) for r in expected_rows]
    act = [normalize_row(r) for r in actual_rows]
    diffs: list[FieldDiff] = []

    if len(act) != len(exp):
        diffs.append(
            FieldDiff(
                row_index=0,
                field="__row_count__",
                expected=len(exp),
                actual=len(act),
            )
        )

    n = min(len(act), len(exp))
    field_total = max(len(exp), 1) * len(REQUIRED_FIELDS)
    field_ok = 0

    for i in range(n):
        for field_name in REQUIRED_FIELDS:
            e = exp[i].get(field_name)
            a = act[i].get(field_name)
            if values_equal(field_name, e, a):
                field_ok += 1
            else:
                diffs.append(
                    FieldDiff(row_index=i + 1, field=field_name, expected=e, actual=a)
                )

    # Penalize missing/extra rows fully
    missing = abs(len(act) - len(exp)) * len(REQUIRED_FIELDS)
    denom = field_total + (missing if len(act) != len(exp) else 0)
    # When lengths differ, field_total uses expected; extra actual rows aren't counted in field_ok
    score = field_ok / denom if denom else 1.0
    passed = len(diffs) == 0
    summary = (
        f"DIU compare score={score:.4f} rows actual={len(act)} expected={len(exp)} "
        f"diffs={len(diffs)} passed={passed}"
    )
    return CompareResult(
        score=score,
        matched_rows=n if passed else max(0, n - len({d.row_index for d in diffs if d.row_index})),
        expected_rows=len(exp),
        actual_rows=len(act),
        diffs=diffs,
        passed=passed,
        summary=summary,
    )
