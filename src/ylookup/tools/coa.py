"""Chart of Accounts lookups: Trans Type → GL account code."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoaEntry:
    account_type: str | None
    gl_account_code: str
    gl_account: str | None
    trans_type: str


def build_coa_index(rows: list[dict]) -> dict[str, CoaEntry]:
    """Index COA rows by exact Trans Type string."""
    index: dict[str, CoaEntry] = {}
    for row in rows:
        trans_type = str(row.get("trans_type") or row.get("Trans Type") or "").strip()
        code = row.get("gl_account_code") or row.get("GL Account Code")
        if not trans_type or code is None:
            continue
        index[trans_type] = CoaEntry(
            account_type=(row.get("account_type") or row.get("Account Type")),
            gl_account_code=str(code),
            gl_account=(row.get("gl_account") or row.get("GL Account")),
            trans_type=trans_type,
        )
    return index


def lookup_account(coa: dict[str, CoaEntry], trans_type: str) -> str:
    entry = coa.get(trans_type)
    if entry is None:
        raise KeyError(f"COA missing Trans Type: {trans_type!r}")
    return entry.gl_account_code
