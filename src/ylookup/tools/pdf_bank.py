"""Extract deposit lines from bank statement PDFs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pdfplumber

_DATE_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})\s+(?P<year>\d{4})\b"
)
_AMOUNT_RE = re.compile(r"([\d,]+\.\d{2})")
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class BankDeposit:
    """A single credit/deposit line from a bank PDF."""

    date: date
    description: str
    amount: float
    raw_line: str = ""


def _parse_date(token: str) -> date | None:
    m = _DATE_RE.match(token.strip())
    if not m:
        return None
    month = _MONTHS.get(m.group("month").lower()[:3])
    if month is None:
        return None
    return date(int(m.group("year")), month, int(m.group("day")))


def _clean_desc(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_deposits(pdf_path: str | Path) -> list[BankDeposit]:
    """Parse deposit rows from a statement PDF (text + amount heuristics)."""
    path = Path(pdf_path)
    deposits: list[BankDeposit] = []
    with pdfplumber.open(path) as pdf:
        full_text = "\n".join((page.extract_text() or "") for page in pdf.pages)

    lines = [ln.rstrip() for ln in full_text.splitlines() if ln.strip()]
    i = 0
    while i < len(lines):
        line = lines[i]
        parsed_date = _parse_date(line)
        if parsed_date is None:
            i += 1
            continue

        chunk = [line]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if _parse_date(nxt) is not None:
                break
            lower = nxt.lower()
            if lower.startswith("thank you for banking") or lower.startswith(
                "generated on"
            ):
                break
            chunk.append(nxt)
            j += 1

        blob = " ".join(chunk)
        amounts = [float(a.replace(",", "")) for a in _AMOUNT_RE.findall(blob)]
        if not amounts:
            i = j
            continue

        deposit_amt = amounts[0]
        eur_m = re.search(r"EUR\|([\d,]+\.\d{2})\|", blob, re.IGNORECASE)
        if eur_m:
            deposit_amt = float(eur_m.group(1).replace(",", ""))

        desc = _DATE_RE.sub("", blob, count=1).strip()
        deposits.append(
            BankDeposit(
                date=parsed_date,
                description=_clean_desc(desc),
                amount=deposit_amt,
                raw_line=_clean_desc(blob),
            )
        )
        i = j

    return deposits


def deposit_to_datetime(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)
