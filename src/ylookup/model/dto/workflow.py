"""Pydantic DTOs for workflow bindings, steps, and clarifications."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Clarification(BaseModel):
    id: str = Field(description="Stable question id used to store and look up answers (e.g. match:0).")
    question: str = Field(description="Human-readable prompt shown when resolving an ambiguity.")
    answer: str | None = Field(
        default=None,
        description="User or policy answer for this clarification; null until resolved.",
    )
    options: list[str] = Field(
        default_factory=list,
        description="Optional multiple-choice values offered when asking the question.",
    )


class BindingHint(BaseModel):
    role: str = Field(description="Logical workbook role this binding identifies (e.g. diu, lp_table, coa).")
    sheet_hints: list[str] = Field(
        default_factory=list,
        description="Candidate sheet names used to locate the role when resolved_sheet is unset.",
    )
    header_hints: list[str] = Field(
        default_factory=list,
        description="Expected header labels that help confirm the correct sheet or table.",
    )
    resolved_sheet: str | None = Field(
        default=None,
        description="Concrete workbook sheet name after resolution; null until matched.",
    )


class WorkflowBindings(BaseModel):
    diu: BindingHint = Field(
        default_factory=lambda: BindingHint(
            role="diu",
            sheet_hints=["DIU", "Data Import Utility", "Data Import"],
            header_hints=[
                "Transaction GL Reference",
                "JE Index",
                "Is Debit",
                "Amount (Local)",
                "Transaction Type",
            ],
        ),
        description="Binding for the DIU output tab written by the workflow.",
    )
    lp_table: BindingHint = Field(
        default_factory=lambda: BindingHint(
            role="lp_table",
            sheet_hints=["Capital Call Tracker", "Tracker", "LP Table", "Investor Schedule"],
            header_hints=["Investor", "ID", "Commitment"],
        ),
        description="Binding for the LP / capital-call tracker table of investors.",
    )
    coa: BindingHint = Field(
        default_factory=lambda: BindingHint(
            role="coa",
            sheet_hints=[
                "PCS Chart of Accounts (Global)",
                "Chart of Accounts",
                "Chart Accounts",
                "COA",
                "GL Accounts",
            ],
            header_hints=["Trans Type", "GL Account Code", "GL Account"],
        ),
        description="Binding for the chart of accounts used to map transaction types to GL codes.",
    )
    fund_constants: BindingHint = Field(
        default_factory=lambda: BindingHint(
            role="fund_constants",
            sheet_hints=["Investors", "Fund Constants", "Capital Call Tracker"],
            header_hints=["Legal Entity", "Legal Entity Domain", "Bank Account", "Deal"],
        ),
        description="Binding for fund-level constants (entity, currency, dates, etc.).",
    )
    diu_legend: BindingHint = Field(
        default_factory=lambda: BindingHint(
            role="diu_legend",
            sheet_hints=["Capital Call Tracker", "Tracker", "DIU Legend"],
            header_hints=["DIU Fields"],
        ),
        description="Binding for the DIU field legend that documents output column meanings.",
    )


class WorkflowStep(BaseModel):
    id: str = Field(description="Unique step id within the workflow (often mirrors op).")
    op: Literal["parse_pdf", "match_payments", "build_diu_rows", "write_diu"] = Field(
        description=(
            "Operation to run: parse_pdf extracts bank payments; match_payments links them to LPs; "
            "build_diu_rows constructs journal entries; write_diu persists rows to Excel."
        ),
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional op-specific parameters passed to the step executor.",
    )


class WorkflowPolicies(BaseModel):
    on_no_match: Literal["leave_unpaid", "abort"] = Field(
        default="leave_unpaid",
        description=(
            "When a bank payment cannot be matched to an LP: leave_unpaid keeps the investor unpaid; "
            "abort stops the run."
        ),
    )
    emit: Literal["all_tracker_investors"] = Field(
        default="all_tracker_investors",
        description="Which investors to emit DIU rows for; currently always all tracker investors.",
    )
    match_min_score: float = Field(
        default=0.55,
        description="Minimum fuzzy-match score required to accept a payment→investor match.",
    )
    match_ambiguity_margin: float = Field(
        default=0.08,
        description="Score gap below which top candidates are treated as ambiguous and need clarification.",
    )
    starting_gl_ref: int = Field(
        default=1,
        description="Starting GL reference number for contiguous journal-entry pairing.",
    )
    cash_trans_type: str = Field(
        default="Cash - Received - USD",
        description="Chart-of-accounts transaction type used for the cash (debit) DIU leg.",
    )
    due_trans_type: str = Field(
        default="Due From Investors",
        description="Chart-of-accounts transaction type used for the due-from (credit) DIU leg.",
    )


class WorkflowValidators(BaseModel):
    row_count_equals_two_x_lps: bool = Field(
        default=True,
        description="Require DIU row count to equal two times the number of LPs (cash + due pair).",
    )
    je_pairs_balance: bool = Field(
        default=True,
        description="Require each cash/due journal-entry pair to balance (equal amounts).",
    )
    coa_accounts_exist: bool = Field(
        default=True,
        description="Require cash and due transaction types to resolve to real COA account codes.",
    )
    gl_refs_contiguous: bool = Field(
        default=True,
        description="Require GL reference numbers to be contiguous starting from starting_gl_ref.",
    )
    match_confidence_gate: bool = Field(
        default=True,
        description="Fail validation when payment matches fall below match_min_score or remain ambiguous.",
    )


class WorkflowDTO(BaseModel):
    """Runtime workflow document."""

    version: int = Field(default=1, description="Workflow schema version.")
    intent: str = Field(
        default="Fill DIU tab for capital call receipts from bank PDF",
        description="Natural-language description of what this workflow is meant to accomplish.",
    )
    bindings: WorkflowBindings = Field(
        default_factory=WorkflowBindings,
        description="Workbook sheet bindings for DIU, LP table, COA, fund constants, and DIU legend.",
    )
    steps: list[WorkflowStep] = Field(
        default_factory=list,
        description="Ordered pipeline of operations executed by the runner.",
    )
    validators: WorkflowValidators = Field(
        default_factory=WorkflowValidators,
        description="Post-run checks that must pass for a successful DIU fill.",
    )
    policies: WorkflowPolicies = Field(
        default_factory=WorkflowPolicies,
        description="Matching, emission, and journal-entry policy knobs used during execution.",
    )
    clarifications: list[Clarification] = Field(
        default_factory=list,
        description="Q&A records capturing ambiguities resolved during build or run.",
    )
    aliases: dict[str, str] = Field(
        default_factory=dict,
        description="Forced payment→investor name mappings keyed by clarification id or bank hint.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form metadata attached by builders or tooling (not used by the runner).",
    )

    def clarification_map(self) -> dict[str, str]:
        return {
            c.id: c.answer
            for c in self.clarifications
            if c.answer is not None and str(c.answer).strip() != ""
        }

    def upsert_clarification(self, item: Clarification) -> None:
        for i, existing in enumerate(self.clarifications):
            if existing.id == item.id:
                self.clarifications[i] = item
                return
        self.clarifications.append(item)


def default_workflow() -> WorkflowDTO:
    return WorkflowDTO(
        steps=[
            WorkflowStep(id="parse_pdf", op="parse_pdf"),
            WorkflowStep(id="match_payments", op="match_payments"),
            WorkflowStep(id="build_diu_rows", op="build_diu_rows"),
            WorkflowStep(id="write_diu", op="write_diu"),
        ]
    )
