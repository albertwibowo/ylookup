"""Execute a saved workflow: bind sheets, run steps, write DIU only."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ylookup.model.dao.workflow import load_workflow
from ylookup.model.dto.workflow import Clarification, WorkflowDTO, default_workflow
from ylookup.tools.diu_build import build_diu_rows
from ylookup.tools.excel import WorkbookProfile, profile_workbook, write_diu_rows
from ylookup.tools.match import PaymentMatch, match_deposits_to_investors
from ylookup.tools.pdf_bank import BankDeposit, extract_deposits

# Roles that must resolve for a successful DIU fill.
_CRITICAL_BIND_ROLES = ("lp_table", "coa", "diu")


@dataclass
class Ambiguity:
    clarification: Clarification
    detail: str = ""


@dataclass
class RunResult:
    workflow: WorkflowDTO
    diu_rows: list[dict[str, Any]] = field(default_factory=list)
    deposits: list[BankDeposit] = field(default_factory=list)
    matches: list[PaymentMatch] = field(default_factory=list)
    output_path: Path | None = None
    ambiguities: list[Ambiguity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    investor_count: int = 0


def resolve_bindings(workflow: WorkflowDTO, profile: WorkbookProfile) -> WorkflowDTO:
    """Sync binding.resolved_sheet from semantic profiling results."""
    role_map = {
        "diu": workflow.bindings.diu,
        "lp_table": workflow.bindings.lp_table,
        "coa": workflow.bindings.coa,
        "fund_constants": workflow.bindings.fund_constants,
        "diu_legend": workflow.bindings.diu_legend,
    }
    for role, binding in role_map.items():
        resolved = profile.resolved_sheets.get(role)
        if resolved:
            binding.resolved_sheet = resolved
        elif binding.resolved_sheet and binding.resolved_sheet not in profile.sheet_names:
            binding.resolved_sheet = None
    return workflow


def _missing_binding_ambiguities(profile: WorkbookProfile, workflow: WorkflowDTO) -> list[Ambiguity]:
    """Ask which sheet maps to a role when semantic resolution failed."""
    out: list[Ambiguity] = []
    clar_map = workflow.clarification_map()
    role_labels = {
        "lp_table": "LP / capital-call tracker (investor list)",
        "coa": "Chart of Accounts (Trans Type → GL account)",
        "diu": "DIU output sheet",
    }
    for role in _CRITICAL_BIND_ROLES:
        if profile.resolved_sheets.get(role):
            continue
        qid = f"bind:{role}"
        if qid in clar_map:
            continue
        out.append(
            Ambiguity(
                clarification=Clarification(
                    id=qid,
                    question=(
                        f"Which workbook sheet is the {role_labels[role]}? "
                        f"Available: {profile.sheet_names}"
                    ),
                    options=list(profile.sheet_names),
                ),
                detail=f"semantic resolve failed for role={role}",
            )
        )
    return out


def _collect_match_ambiguities(matches: list[PaymentMatch]) -> list[Ambiguity]:
    out: list[Ambiguity] = []
    for m in matches:
        if not m.ambiguous:
            continue
        opts = [c.investor_name for c in m.candidates[:3]]
        hint = m.clarification_id.removeprefix("match:")
        question = (
            f'Bank "{hint}" / {m.deposit.amount} on {m.deposit.date} → '
            f"which investor? candidates={opts}"
        )
        out.append(
            Ambiguity(
                clarification=Clarification(
                    id=m.clarification_id,
                    question=question,
                    options=opts + ["skip"],
                ),
                detail=f"scores={[round(c.score, 3) for c in m.candidates[:3]]}",
            )
        )
    return out


def execute_workflow(
    workflow: WorkflowDTO,
    *,
    input_xlsx: str | Path,
    pdf_path: str | Path | None,
    output_xlsx: str | Path | None = None,
    collect_ambiguities: bool = True,
) -> RunResult:
    """Run workflow steps. Never prompts; uses workflow clarifications/aliases."""
    result = RunResult(workflow=workflow)

    # Apply any prior bind:* answers before profiling so renamed sheets stick.
    for qid, answer in workflow.clarification_map().items():
        if not qid.startswith("bind:"):
            continue
        role = qid.removeprefix("bind:")
        binding = getattr(workflow.bindings, role, None)
        if binding is not None and answer:
            binding.resolved_sheet = answer

    profile = profile_workbook(input_xlsx, bindings=workflow.bindings)
    workflow = resolve_bindings(workflow, profile)
    result.workflow = workflow
    result.investor_count = len(profile.tracker_investors)

    bind_ambiguities = _missing_binding_ambiguities(profile, workflow)
    if collect_ambiguities and bind_ambiguities:
        result.ambiguities.extend(bind_ambiguities)
        # Defer hard errors until sheet roles are chosen.
        return result

    unresolved_roles = [r for r in _CRITICAL_BIND_ROLES if not profile.resolved_sheets.get(r)]
    if unresolved_roles:
        result.errors.append(
            "Unresolved sheet bindings: " + ", ".join(unresolved_roles)
        )
        return result

    if not profile.tracker_investors:
        result.errors.append("No tracker investors found")
        return result

    if not profile.coa_rows:
        result.errors.append("Chart of Accounts resolved but contains no usable rows")
        return result

    policies = workflow.policies
    clar_map = workflow.clarification_map()
    deposits: list[BankDeposit] = []
    matches: list[PaymentMatch] = []
    diu_rows: list[dict[str, Any]] = []

    # Merge policy constants into fund constants for DIU build.
    fund_constants = dict(profile.fund_constants)
    fund_constants["cash_trans_type"] = policies.cash_trans_type
    fund_constants["due_trans_type"] = policies.due_trans_type
    fund_constants["starting_gl_ref"] = policies.starting_gl_ref

    for step in workflow.steps:
        if step.op == "parse_pdf":
            if pdf_path is None:
                result.errors.append("PDF path required for parse_pdf")
                if collect_ambiguities:
                    result.ambiguities.append(
                        Ambiguity(
                            clarification=Clarification(
                                id="input:missing_pdf",
                                question="Bank PDF is missing. Provide a path or abort?",
                                options=["abort"],
                            )
                        )
                    )
                return result
            deposits = extract_deposits(pdf_path)
            if not deposits and collect_ambiguities:
                result.ambiguities.append(
                    Ambiguity(
                        clarification=Clarification(
                            id="pdf:empty_payments",
                            question="No deposits parsed from PDF. Continue with all unpaid or abort?",
                            options=["leave_unpaid", "abort"],
                        )
                    )
                )
            result.deposits = deposits

        elif step.op == "match_payments":
            matches = match_deposits_to_investors(
                deposits,
                profile.tracker_investors,
                min_score=policies.match_min_score,
                ambiguity_margin=policies.match_ambiguity_margin,
                aliases=workflow.aliases,
                clarifications=clar_map,
            )
            result.matches = matches
            if collect_ambiguities:
                result.ambiguities.extend(_collect_match_ambiguities(matches))

        elif step.op == "build_diu_rows":
            try:
                diu_rows = build_diu_rows(
                    profile.tracker_investors,
                    matches,
                    fund_constants,
                    profile.coa_rows,
                    emit=policies.emit,
                    on_no_match=policies.on_no_match,
                    starting_gl_ref=policies.starting_gl_ref,
                )
            except Exception as exc:  # noqa: BLE001 — surface to CLI report
                result.errors.append(str(exc))
                return result
            result.diu_rows = diu_rows

        elif step.op == "write_diu":
            if output_xlsx is None:
                result.errors.append("output path required for write_diu")
                return result
            if not diu_rows:
                result.errors.append("No DIU rows to write")
                return result
            path = write_diu_rows(
                input_xlsx,
                output_xlsx,
                diu_rows,
                diu_sheet=workflow.bindings.diu.resolved_sheet or "DIU",
            )
            result.output_path = path
        else:
            result.errors.append(f"Unknown step op: {step.op}")

    # Runtime unresolved ambiguity for run mode (collect_ambiguities False-ish check by caller)
    return result


def run_saved_workflow(
    workflow_path: str | Path,
    *,
    input_xlsx: str | Path,
    pdf_path: str | Path,
    output_xlsx: str | Path,
) -> RunResult:
    workflow = load_workflow(workflow_path)
    result = execute_workflow(
        workflow,
        input_xlsx=input_xlsx,
        pdf_path=pdf_path,
        output_xlsx=output_xlsx,
        collect_ambiguities=True,
    )
    # run never prompts: treat remaining ambiguities as hard failure
    unanswered = [
        a
        for a in result.ambiguities
        if a.clarification.id not in workflow.clarification_map()
    ]
    if unanswered:
        result.errors.append(
            "Unresolved ambiguity at run time (workflow missing clarifications): "
            + ", ".join(a.clarification.id for a in unanswered)
        )
    return result


def heuristic_propose_workflow() -> WorkflowDTO:
    return default_workflow()
