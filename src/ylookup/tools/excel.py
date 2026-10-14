"""Excel helpers: read workbook sheets and write DIU data rows only."""

from __future__ import annotations

import re
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

if TYPE_CHECKING:
    from ylookup.model.dto.workflow import BindingHint, WorkflowBindings

_SHEET_STOPWORDS = frozenset({"of", "the", "a", "an", "and", "or", "for", "to", "in", "on"})

DIU_HEADERS = [
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


@dataclass
class SheetFingerprint:
    """Lightweight content fingerprint used for semantic sheet matching."""

    name: str
    labels: set[str] = field(default_factory=set)  # lowercased cell strings


@dataclass
class WorkbookProfile:
    sheet_names: list[str]
    diu_headers: list[str]
    tracker_investors: list[dict[str, Any]]
    fund_constants: dict[str, Any]
    coa_rows: list[dict[str, Any]]
    diu_legend: dict[str, Any]
    investors_sheet: dict[str, Any]
    resolved_sheets: dict[str, str | None] = field(default_factory=dict)
    fingerprints: list[SheetFingerprint] = field(default_factory=list)


def _cell_value(ws: Worksheet, row: int, col: int) -> Any:
    return ws.cell(row, col).value


def _sheet_tokens(name: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", name.lower())
    return {t for t in toks if t not in _SHEET_STOPWORDS and len(t) > 1}


def fingerprint_sheet(ws: Worksheet, *, max_row: int = 60, max_col: int = 40) -> SheetFingerprint:
    labels: set[str] = set()
    row_limit = min(ws.max_row or 1, max_row)
    col_limit = min(ws.max_column or 1, max_col)
    for r in range(1, row_limit + 1):
        for c in range(1, col_limit + 1):
            value = ws.cell(r, c).value
            if value is None:
                continue
            text = str(value).strip()
            if text:
                labels.add(text.lower())
    return SheetFingerprint(name=ws.title, labels=labels)


def name_match_score(sheet_name: str, candidates: list[str]) -> float:
    """Score how well a sheet name matches role name hints (0..1)."""
    if not candidates:
        return 0.0
    lower = sheet_name.lower()
    best = 0.0
    name_toks = _sheet_tokens(sheet_name)
    for candidate in candidates:
        cand = candidate.lower()
        if lower == cand:
            return 1.0
        if cand in lower or lower in cand:
            best = max(best, 0.9)
            continue
        hint_toks = _sheet_tokens(candidate)
        if not hint_toks or not name_toks:
            continue
        inter = hint_toks & name_toks
        if not inter:
            continue
        coverage = len(inter) / len(hint_toks)
        jaccard = len(inter) / len(hint_toks | name_toks)
        if coverage >= 0.5 or len(inter) >= 2:
            best = max(best, 0.55 * coverage + 0.35 * jaccard)
    return best


def content_match_score(fp: SheetFingerprint, header_hints: list[str]) -> float:
    """Score how many expected headers/labels appear in the sheet (0..1)."""
    if not header_hints or not fp.labels:
        return 0.0
    hits = 0.0
    for hint in header_hints:
        needle = hint.strip().lower()
        if not needle:
            continue
        if needle in fp.labels:
            hits += 1.0
            continue
        # Substring / near-label match (e.g. renamed column still contains key phrase)
        if any(needle in label or label in needle for label in fp.labels):
            hits += 0.9
            continue
        hint_toks = _sheet_tokens(hint)
        if len(hint_toks) >= 2 and any(hint_toks <= _sheet_tokens(label) for label in fp.labels):
            hits += 0.75
    return min(1.0, hits / len(header_hints))


def score_sheet_for_role(
    fp: SheetFingerprint,
    *,
    sheet_hints: list[str],
    header_hints: list[str],
) -> float:
    """Combine name + content signals. Content dominates when headers are available."""
    name = name_match_score(fp.name, sheet_hints)
    content = content_match_score(fp, header_hints)
    if header_hints:
        # Content is the semantic signal for renamed sheets.
        return 0.3 * name + 0.7 * content
    return name


def resolve_sheet_for_role(
    fingerprints: list[SheetFingerprint],
    *,
    sheet_hints: list[str],
    header_hints: list[str] | None = None,
    preferred: str | None = None,
    exclude: set[str] | None = None,
    min_score: float = 0.35,
) -> str | None:
    """Pick the best sheet for a role using preferred name, then semantic score."""
    header_hints = header_hints or []
    exclude = exclude or set()
    by_name = {fp.name: fp for fp in fingerprints}

    if preferred and preferred in by_name and preferred not in exclude:
        # Still verify preferred is not wildly wrong when we have content hints.
        if header_hints:
            pref_score = score_sheet_for_role(
                by_name[preferred], sheet_hints=sheet_hints, header_hints=header_hints
            )
            if pref_score >= min_score:
                return preferred
        else:
            return preferred

    def hint_rank(sheet_name: str) -> int:
        """Lower is better: earlier sheet_hints are preferred on ties."""
        lower = sheet_name.lower()
        for idx, hint in enumerate(sheet_hints):
            h = hint.lower()
            if lower == h or h in lower or lower in h:
                return idx
        return len(sheet_hints) + 1

    best_name: str | None = None
    best_score = -1.0
    best_rank = 10**9
    for fp in fingerprints:
        if fp.name in exclude:
            continue
        score = score_sheet_for_role(fp, sheet_hints=sheet_hints, header_hints=header_hints)
        rank = hint_rank(fp.name)
        if score > best_score + 1e-12 or (abs(score - best_score) <= 1e-12 and rank < best_rank):
            best_score = score
            best_name = fp.name
            best_rank = rank
    if best_name is None or best_score < min_score:
        return None
    return best_name


def match_sheet_name(
    sheet_names: list[str],
    candidates: list[str],
    *,
    fingerprints: list[SheetFingerprint] | None = None,
    header_hints: list[str] | None = None,
) -> str | None:
    """Resolve a sheet from name hints, optionally using content fingerprints."""
    if fingerprints is not None:
        return resolve_sheet_for_role(
            fingerprints,
            sheet_hints=candidates,
            header_hints=header_hints or [],
        )
    # Name-only fallback (no workbook content available).
    synthetic = [SheetFingerprint(name=n) for n in sheet_names]
    return resolve_sheet_for_role(synthetic, sheet_hints=candidates, header_hints=[])


def _find_sheet(wb, candidates: list[str], header_hints: list[str] | None = None) -> str | None:
    fingerprints = [fingerprint_sheet(wb[name]) for name in wb.sheetnames]
    return resolve_sheet_for_role(
        fingerprints,
        sheet_hints=candidates,
        header_hints=header_hints or [],
    )


def _binding_hints(bindings: WorkflowBindings | None) -> dict[str, BindingHint]:
    """Merge workflow bindings with durable default name/header synonyms."""
    from ylookup.model.dto.workflow import BindingHint as BH
    from ylookup.model.dto.workflow import WorkflowBindings as WB

    defaults = WB()
    provided = bindings or WB()

    def merge(role: str) -> BH:
        cur = getattr(provided, role)
        dflt = getattr(defaults, role)
        # Prefer workflow hints first, then fall back to built-in synonyms/headers.
        sheet_hints = list(dict.fromkeys([*(cur.sheet_hints or []), *(dflt.sheet_hints or [])]))
        header_hints = list(dict.fromkeys([*(cur.header_hints or []), *(dflt.header_hints or [])]))
        return BH(
            role=cur.role or dflt.role,
            sheet_hints=sheet_hints,
            header_hints=header_hints,
            resolved_sheet=cur.resolved_sheet,
        )

    return {role: merge(role) for role in ("diu", "lp_table", "coa", "fund_constants", "diu_legend")}


def read_tracker_investors(ws: Worksheet) -> list[dict[str, Any]]:
    """Read LP rows from Capital Call Tracker (header row with Investor column)."""
    header_row = None
    headers: dict[str, int] = {}
    for r in range(1, min(ws.max_row or 1, 40) + 1):
        row_vals = {
            str(_cell_value(ws, r, c)).strip(): c
            for c in range(1, min(ws.max_column or 1, 30) + 1)
            if _cell_value(ws, r, c) is not None
        }
        if "Investor" in row_vals and ("ID" in row_vals or "Commitment" in row_vals):
            header_row = r
            headers = row_vals
            break
    if header_row is None:
        return []

    inv_col = headers["Investor"]
    id_col = headers.get("ID")
    investors: list[dict[str, Any]] = []
    for r in range(header_row + 1, (ws.max_row or header_row) + 1):
        name = _cell_value(ws, r, inv_col)
        if name is None or not str(name).strip():
            # Stop at totals / blank block
            if investors:
                break
            continue
        # Skip currency subtitle rows
        if str(name).strip().upper() in {"USD", "EUR", "GBP"}:
            continue
        inv_id = _cell_value(ws, r, id_col) if id_col else None
        # Skip if ID missing and name looks like a total label
        if id_col and inv_id is None:
            break
        investors.append(
            {
                "id": str(inv_id).strip() if inv_id is not None else None,
                "name": str(name).strip(),
                "row": r,
            }
        )
    return investors


def read_fund_constants(ws: Worksheet, investors_ws: Worksheet | None) -> dict[str, Any]:
    """Pull Legal Entity / Deal / Position / Bank Account / currency from sheets."""
    constants: dict[str, Any] = {
        "legal_entity": _cell_value(ws, 1, 1),
        "transaction_currency": "USD",
        "batch_index": 1,
        "batch_type": "General",
        "cash_trans_type": "Cash - Received - USD",
        "due_trans_type": "Due From Investors",
        "alloc_cash": "Non-Dominant",
        "alloc_due": "User Provided",
        "starting_gl_ref": 1,
    }
    # Legend block on tracker
    for r in range(1, min(ws.max_row or 1, 60) + 1):
        key = _cell_value(ws, r, 1)
        val = _cell_value(ws, r, 2)
        if key is None:
            continue
        k = str(key).strip()
        if k == "Legal Entity" and val:
            constants["legal_entity"] = val
        elif k == "Deal" and val:
            constants["deal"] = val
        elif k == "Position" and val:
            constants["position"] = val
        elif k == "Transaction Currency" and val:
            constants["transaction_currency"] = val
        elif k == "Transaction Type" and val:
            constants["cash_trans_type"] = val
        elif k == "Batch Type" and val:
            constants["batch_type"] = val

    if investors_ws is not None:
        # Row 2 of Investors sheet holds fund-level constants in sample.
        headers = {
            str(investors_ws.cell(1, c).value).strip(): c
            for c in range(1, (investors_ws.max_column or 1) + 1)
            if investors_ws.cell(1, c).value is not None
        }
        row = 2
        mapping = {
            "Legal Entity": "legal_entity",
            "Legal Entity Domain": "legal_entity_domain",
            "Deal": "deal",
            "Position": "position",
            "Batch Type": "batch_type",
            "Bank Account": "bank_account",
        }
        for header, dest in mapping.items():
            col = headers.get(header)
            if col:
                val = investors_ws.cell(row, col).value
                if val is not None and str(val).strip():
                    constants[dest] = val

    constants.setdefault("legal_entity_domain", "Test")
    constants.setdefault("deal", "ZZZ Operations USD")
    constants.setdefault("position", "ZZZ Operations USD (zzz Admin position)")
    constants.setdefault("bank_account", None)
    return constants


def read_diu_legend(ws: Worksheet) -> dict[str, Any]:
    legend: dict[str, Any] = {}
    for r in range(1, min(ws.max_row or 1, 80) + 1):
        key = _cell_value(ws, r, 1)
        if key is None:
            continue
        legend[str(key).strip()] = {
            "value": _cell_value(ws, r, 2),
            "note": _cell_value(ws, r, 4),
        }
    return legend


def read_coa(ws: Worksheet) -> list[dict[str, Any]]:
    headers = [ws.cell(1, c).value for c in range(1, (ws.max_column or 1) + 1)]
    rows: list[dict[str, Any]] = []
    for r in range(2, (ws.max_row or 1) + 1):
        item = {
            "account_type": ws.cell(r, 1).value,
            "gl_account_code": ws.cell(r, 2).value,
            "gl_account": ws.cell(r, 3).value,
            "trans_type": ws.cell(r, 4).value,
            "Account Type": ws.cell(r, 1).value,
            "GL Account Code": ws.cell(r, 2).value,
            "GL Account": ws.cell(r, 3).value,
            "Trans Type": ws.cell(r, 4).value,
        }
        if item["trans_type"] is None:
            continue
        rows.append(item)
    # Keep headers for debugging
    _ = headers
    return rows


def read_diu_rows(path: str | Path, sheet_name: str | None = None) -> list[dict[str, Any]]:
    wb = load_workbook(path, data_only=True)
    name = sheet_name or _find_sheet(wb, ["DIU"]) or "DIU"
    ws = wb[name]
    headers = [
        str(ws.cell(1, c).value).strip() if ws.cell(1, c).value is not None else f"COL{c}"
        for c in range(1, (ws.max_column or 1) + 1)
    ]
    rows: list[dict[str, Any]] = []
    for r in range(2, (ws.max_row or 1) + 1):
        row = {headers[c - 1]: ws.cell(r, c).value for c in range(1, len(headers) + 1)}
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in row.values()):
            continue
        rows.append(row)
    return rows


def profile_workbook(
    path: str | Path,
    *,
    bindings: WorkflowBindings | None = None,
) -> WorkbookProfile:
    """Profile a workbook, resolving role sheets by name + content semantics."""
    wb = load_workbook(path, data_only=True)
    fingerprints = [fingerprint_sheet(wb[name]) for name in wb.sheetnames]
    hints = _binding_hints(bindings)

    # Resolve exclusive structural roles first, then roles that may share sheets.
    exclusive: set[str] = set()
    coa_name = resolve_sheet_for_role(
        fingerprints,
        sheet_hints=hints["coa"].sheet_hints,
        header_hints=hints["coa"].header_hints,
        preferred=hints["coa"].resolved_sheet,
        exclude=exclusive,
    )
    if coa_name:
        exclusive.add(coa_name)

    diu_name = resolve_sheet_for_role(
        fingerprints,
        sheet_hints=hints["diu"].sheet_hints,
        header_hints=hints["diu"].header_hints,
        preferred=hints["diu"].resolved_sheet,
        exclude=exclusive,
    )
    if diu_name:
        exclusive.add(diu_name)

    tracker_name = resolve_sheet_for_role(
        fingerprints,
        sheet_hints=hints["lp_table"].sheet_hints,
        header_hints=hints["lp_table"].header_hints,
        preferred=hints["lp_table"].resolved_sheet,
        exclude=exclusive,
    )
    if tracker_name:
        exclusive.add(tracker_name)

    structural = {n for n in (coa_name, diu_name) if n}

    # Investors / fund constants: prefer a dedicated sheet; may share with tracker.
    inv_name = resolve_sheet_for_role(
        fingerprints,
        sheet_hints=hints["fund_constants"].sheet_hints,
        header_hints=hints["fund_constants"].header_hints,
        preferred=hints["fund_constants"].resolved_sheet,
        exclude=structural,
    )

    legend_name = resolve_sheet_for_role(
        fingerprints,
        sheet_hints=hints["diu_legend"].sheet_hints,
        header_hints=hints["diu_legend"].header_hints,
        preferred=hints["diu_legend"].resolved_sheet,
        exclude=structural,
    ) or tracker_name

    tracker_ws = wb[tracker_name] if tracker_name else None
    inv_ws = wb[inv_name] if inv_name else None
    coa_ws = wb[coa_name] if coa_name else None
    diu_ws = wb[diu_name] if diu_name else None
    legend_ws = wb[legend_name] if legend_name else tracker_ws

    diu_headers = DIU_HEADERS[:]
    if diu_ws is not None:
        diu_headers = [
            str(diu_ws.cell(1, c).value).strip()
            for c in range(1, (diu_ws.max_column or 1) + 1)
            if diu_ws.cell(1, c).value is not None
        ]

    investors = read_tracker_investors(tracker_ws) if tracker_ws else []
    constants = (
        read_fund_constants(tracker_ws, inv_ws)
        if tracker_ws
        else {"legal_entity": None, "batch_index": 1}
    )
    legend = read_diu_legend(legend_ws) if legend_ws else {}
    coa_rows = read_coa(coa_ws) if coa_ws else []
    inv_sheet: dict[str, Any] = {}
    if inv_ws is not None:
        inv_sheet = {
            "headers": [inv_ws.cell(1, c).value for c in range(1, (inv_ws.max_column or 1) + 1)],
            "row2": [inv_ws.cell(2, c).value for c in range(1, (inv_ws.max_column or 1) + 1)],
        }

    return WorkbookProfile(
        sheet_names=list(wb.sheetnames),
        diu_headers=diu_headers,
        tracker_investors=investors,
        fund_constants=constants,
        coa_rows=coa_rows,
        diu_legend=legend,
        investors_sheet=inv_sheet,
        resolved_sheets={
            "diu": diu_name,
            "lp_table": tracker_name,
            "coa": coa_name,
            "fund_constants": inv_name,
            "diu_legend": legend_name,
        },
        fingerprints=fingerprints,
    )


def write_diu_rows(
    input_xlsx: str | Path,
    output_xlsx: str | Path,
    rows: list[dict[str, Any]],
    *,
    diu_sheet: str = "DIU",
) -> Path:
    """Copy input workbook and replace DIU data rows (keep header)."""
    in_path = Path(input_xlsx)
    out_path = Path(output_xlsx)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = load_workbook(in_path)
    sheet_name = _find_sheet(wb, [diu_sheet, "DIU"]) or diu_sheet
    if sheet_name not in wb.sheetnames:
        ws = wb.create_sheet(sheet_name)
        for c, h in enumerate(DIU_HEADERS, start=1):
            ws.cell(1, c, h)
    else:
        ws = wb[sheet_name]

    headers = [
        str(ws.cell(1, c).value).strip() if ws.cell(1, c).value is not None else f"COL{c}"
        for c in range(1, max(ws.max_column or 1, len(DIU_HEADERS)) + 1)
        if ws.cell(1, c).value is not None or c <= len(DIU_HEADERS)
    ]
    # Prefer existing header row; fall back to canonical.
    if not any(ws.cell(1, c).value for c in range(1, 10)):
        headers = DIU_HEADERS[:]
        for c, h in enumerate(headers, start=1):
            ws.cell(1, c, h)
    else:
        headers = [
            str(ws.cell(1, c).value).strip()
            for c in range(1, (ws.max_column or 1) + 1)
            if ws.cell(1, c).value is not None
        ]

    # Clear existing data rows only.
    if ws.max_row and ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)

    for r_idx, row in enumerate(rows, start=2):
        for c_idx, header in enumerate(headers, start=1):
            value = row.get(header)
            if isinstance(value, datetime):
                ws.cell(r_idx, c_idx, value)
            else:
                ws.cell(r_idx, c_idx, value)

    wb.save(out_path)
    return out_path


def copy_cell_styles(src, dest) -> None:
    if src.has_style:
        dest.font = copy(src.font)
        dest.border = copy(src.border)
        dest.fill = copy(src.fill)
        dest.number_format = src.number_format
        dest.protection = copy(src.protection)
        dest.alignment = copy(src.alignment)
