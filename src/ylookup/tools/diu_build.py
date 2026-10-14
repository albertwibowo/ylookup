"""Expand tracker LPs + matched payments into DIU journal rows."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ylookup.tools.coa import build_coa_index, lookup_account
from ylookup.tools.match import PaymentMatch, payments_by_investor
def _as_dt(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def build_diu_rows(
    investors: list[dict[str, Any]],
    matches: list[PaymentMatch],
    fund_constants: dict[str, Any],
    coa_rows: list[dict[str, Any]],
    *,
    emit: str = "all_tracker_investors",
    on_no_match: str = "leave_unpaid",
    starting_gl_ref: int | None = None,
) -> list[dict[str, Any]]:
    """Build 2 DIU rows per investor (cash debit + due-from credit).

    Unpaid investors get amount 0 and blank GL/Effective dates.
    JE Index follows tracker order. Transaction GL Reference is contiguous 1..N.
    """
    if emit != "all_tracker_investors":
        raise ValueError(f"Unsupported emit policy: {emit}")

    coa = build_coa_index(coa_rows)
    cash_tt = str(fund_constants.get("cash_trans_type") or "Cash - Received - USD")
    due_tt = str(fund_constants.get("due_trans_type") or "Due From Investors")
    cash_acct = lookup_account(coa, cash_tt)
    due_acct = lookup_account(coa, due_tt)

    legal_entity = fund_constants.get("legal_entity")
    legal_entity_domain = fund_constants.get("legal_entity_domain")
    deal = fund_constants.get("deal")
    position = fund_constants.get("position")
    batch_index = fund_constants.get("batch_index", 1)
    batch_type = fund_constants.get("batch_type") or "General"
    currency = fund_constants.get("transaction_currency") or "USD"
    bank_account = fund_constants.get("bank_account")
    alloc_cash = fund_constants.get("alloc_cash") or "Non-Dominant"
    alloc_due = fund_constants.get("alloc_due") or "User Provided"
    gl_ref = int(
        starting_gl_ref
        if starting_gl_ref is not None
        else fund_constants.get("starting_gl_ref", 1)
    )

    by_name = payments_by_investor(matches)
    rows: list[dict[str, Any]] = []

    for je_index, inv in enumerate(investors, start=1):
        name = str(inv["name"])
        payment = by_name.get(name)
        paid = payment is not None
        if not paid and on_no_match == "abort":
            raise RuntimeError(f"No payment match for investor {name!r}")

        if paid:
            amount = float(payment.deposit.amount)
            gl_date = _as_dt(payment.deposit.date)
            effective = gl_date
        else:
            amount = 0.0
            gl_date = None
            effective = None

        comment = f"Capital Call Received from {name}"

        # Trans Index 1 — cash received (debit)
        cash_row: dict[str, Any] = {
            "Legal Entity": legal_entity,
            "Legal Entity Domain": legal_entity_domain,
            "Deal": deal,
            "Position": position,
            "Batch Index": batch_index,
            "Batch Type": batch_type,
            "JE Index": je_index,
            "Trans Index": 1,
            "GL Date": gl_date,
            "Effective Date": effective,
            "Transaction Currency": currency,
            "Allocation Rule": alloc_cash,
            "Transaction Type": cash_tt,
            "Account": cash_acct,
            "Is Debit": "Yes",
            "Amount (Local)": amount,
            "Amount (LE)": amount,
            "Transaction Comments": comment,
            "Batch Comments": comment,
            "Bank Account": bank_account,
            "Investor": None,
            "Investor Domain": None,
            "Vehicle": None,
            "Investor Amount (LE)": None,
            "Investor Amount (Local)": None,
            "Transaction GL Reference": gl_ref,
        }
        gl_ref += 1

        # Trans Index 2 — due from investors (credit)
        due_row: dict[str, Any] = {
            "Legal Entity": legal_entity,
            "Legal Entity Domain": legal_entity_domain,
            "Deal": deal,
            "Position": position,
            "Batch Index": batch_index,
            "Batch Type": batch_type,
            "JE Index": je_index,
            "Trans Index": 2,
            "GL Date": gl_date,
            "Effective Date": effective,
            "Transaction Currency": currency,
            "Allocation Rule": alloc_due,
            "Transaction Type": due_tt,
            "Account": due_acct,
            "Is Debit": "No",
            "Amount (Local)": amount,
            "Amount (LE)": amount,
            "Transaction Comments": comment,
            "Batch Comments": comment,
            "Bank Account": None,
            "Investor": name,
            "Investor Domain": None,
            "Vehicle": legal_entity,
            "Investor Amount (LE)": amount,
            "Investor Amount (Local)": amount,
            "Transaction GL Reference": gl_ref,
        }
        gl_ref += 1
        rows.extend([cash_row, due_row])

    return rows
