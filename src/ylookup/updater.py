"""Update loop: load workflow → clear stale binds → adapt → execute/ask/compare/patch."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from ylookup.ask import AskBudget, UserAbort, ask_user, env_int
from ylookup.builder import BuildReport, _apply_answers, _unresolved
from ylookup.compare.diu import CompareResult, compare_diu
from ylookup.llm import adapt_workflow, patch_workflow
from ylookup.model.dao.workflow import load_workflow, save_workflow
from ylookup.model.dto.workflow import BindingHint, WorkflowBindings, WorkflowDTO
from ylookup.runner import execute_workflow
from ylookup.tools.excel import (
    _merge_column_aliases,
    name_match_score,
    profile_workbook,
    read_diu_rows,
    resolve_header,
)


def _profile_summary(input_xlsx: Path, workflow: WorkflowDTO | None = None) -> dict[str, Any]:
    """Richer profile for update: include per-sheet fingerprint labels for column aliases."""
    profile = profile_workbook(input_xlsx, bindings=workflow.bindings if workflow else None)
    sheet_labels: dict[str, list[str]] = {}
    for fp in profile.fingerprints:
        # Cap labels so the LLM prompt stays bounded.
        sheet_labels[fp.name] = sorted(fp.labels)[:80]
    return {
        "sheets": profile.sheet_names,
        "resolved_sheets": profile.resolved_sheets,
        "diu_headers": profile.diu_headers,
        "investor_count": len(profile.tracker_investors),
        "investors": [i["name"] for i in profile.tracker_investors],
        "fund_constants": {
            k: (str(v) if v is not None else None)
            for k, v in profile.fund_constants.items()
        },
        "coa_sample_types": [
            str(r.get("trans_type"))
            for r in profile.coa_rows
            if str(r.get("trans_type") or "")
            in {"Cash - Received - USD", "Due From Investors"}
        ],
        "unresolved_columns": profile.unresolved_columns,
        "role_headers": profile.role_headers,
        "sheet_labels": sheet_labels,
    }


def _prepare_for_update(workflow: WorkflowDTO, sheet_names: list[str]) -> WorkflowDTO:
    """Clear stale resolved_sheet / bind:* answers; keep still-valid clarifications."""
    sheets = set(sheet_names)
    role_map = {
        "diu": workflow.bindings.diu,
        "lp_table": workflow.bindings.lp_table,
        "coa": workflow.bindings.coa,
        "fund_constants": workflow.bindings.fund_constants,
        "diu_legend": workflow.bindings.diu_legend,
    }
    for binding in role_map.values():
        if binding.resolved_sheet and binding.resolved_sheet not in sheets:
            binding.resolved_sheet = None

    kept = []
    for clar in workflow.clarifications:
        if clar.id.startswith("bind:") and clar.answer:
            if clar.answer not in sheets:
                # Invalidate stale sheet preference; drop sticky answer.
                role = clar.id.removeprefix("bind:")
                binding = role_map.get(role)
                if binding is not None and binding.resolved_sheet == clar.answer:
                    binding.resolved_sheet = None
                clar = clar.model_copy(update={"answer": None})
        kept.append(clar)
    workflow.clarifications = kept
    return workflow


def _extend_aliases_from_labels(
    binding: BindingHint,
    labels: list[str],
) -> None:
    """Append newly observed headers that fuzzy-match default/current aliases."""
    if not labels:
        return
    for field_name, aliases in list(binding.column_aliases.items()):
        matched = resolve_header(labels, aliases)
        if not matched:
            continue
        if matched not in aliases:
            binding.column_aliases[field_name] = [matched, *aliases]
        if matched not in binding.header_hints:
            binding.header_hints = [matched, *binding.header_hints]


def heuristic_adapt_workflow(workflow: WorkflowDTO, input_xlsx: Path) -> WorkflowDTO:
    """No-API adapt: rematch sheets via profile; extend column_aliases from observed headers."""
    defaults = WorkflowBindings()
    profile = profile_workbook(input_xlsx, bindings=workflow.bindings)
    role_map = {
        "diu": workflow.bindings.diu,
        "lp_table": workflow.bindings.lp_table,
        "coa": workflow.bindings.coa,
        "fund_constants": workflow.bindings.fund_constants,
        "diu_legend": workflow.bindings.diu_legend,
    }
    for role, binding in role_map.items():
        dflt = getattr(defaults, role)
        # Seed durable defaults onto the workflow so saved JSON carries synonyms.
        binding.column_aliases = _merge_column_aliases(binding.column_aliases, dflt.column_aliases)
        binding.sheet_hints = list(
            dict.fromkeys([*(binding.sheet_hints or []), *(dflt.sheet_hints or [])])
        )
        binding.header_hints = list(
            dict.fromkeys([*(binding.header_hints or []), *(dflt.header_hints or [])])
        )

        resolved = profile.resolved_sheets.get(role)
        if resolved:
            binding.resolved_sheet = resolved
            if resolved not in binding.sheet_hints:
                binding.sheet_hints = [resolved, *binding.sheet_hints]

        headers = profile.role_headers.get(role) or []
        if not headers:
            fp = next((f for f in profile.fingerprints if f.name == resolved), None)
            if fp:
                headers = sorted(fp.labels)
        if headers:
            _extend_aliases_from_labels(binding, headers)
            for h in headers:
                if h not in binding.header_hints and any(
                    name_match_score(h, aliases) >= 0.55
                    for aliases in binding.column_aliases.values()
                ):
                    binding.header_hints = [*binding.header_hints, h]
    return workflow


def update_workflow(
    *,
    workflow_path: str | Path,
    input_xlsx: str | Path,
    pdf_path: str | Path,
    target_xlsx: str | Path,
    out_workflow: str | Path,
    context_path: str | Path | None = None,
    non_interactive: bool = False,
) -> BuildReport:
    workflow_path = Path(workflow_path)
    input_xlsx = Path(input_xlsx)
    pdf_path = Path(pdf_path)
    target_xlsx = Path(target_xlsx)
    out_workflow = Path(out_workflow)

    max_iters = env_int("MAX_BUILD_ITERS", 3)
    ask_budget = AskBudget()
    context = Path(context_path).read_text(encoding="utf-8") if context_path else None

    workflow = load_workflow(workflow_path)
    # Snapshot sheet names for stale-bind clearing (name-only pass).
    initial_profile = profile_workbook(input_xlsx, bindings=workflow.bindings)
    workflow = _prepare_for_update(workflow, initial_profile.sheet_names)

    summary = _profile_summary(input_xlsx, workflow)
    try:
        workflow = adapt_workflow(workflow, summary, context)
    except Exception:  # noqa: BLE001
        pass
    # Always rematch sheets / extend aliases from observed headers (works without API key).
    workflow = heuristic_adapt_workflow(workflow, input_xlsx)

    # Refresh summary after adapt so patch sees current aliases/headers.
    summary = _profile_summary(input_xlsx, workflow)

    target_rows = read_diu_rows(target_xlsx)
    prev_score = -1.0
    last_compare: CompareResult | None = None
    all_answers: dict[str, str] = dict(workflow.clarification_map())
    asked_ids: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ylookup-update-") as tmp:
        tmp_out = Path(tmp) / "diu-candidate.xlsx"

        for iteration in range(1, max_iters + 1):
            result = execute_workflow(
                workflow,
                input_xlsx=input_xlsx,
                pdf_path=pdf_path,
                output_xlsx=tmp_out,
                collect_ambiguities=True,
            )
            if result.errors and not result.diu_rows and not result.ambiguities:
                report = BuildReport(
                    stop_reason="execute_error",
                    exit_code=1,
                    iterations=iteration,
                    ask_rounds=ask_budget.rounds_used,
                    failing_checks=result.errors,
                    message="; ".join(result.errors),
                )
                report.print_report()
                return report

            unresolved = _unresolved(result.ambiguities, workflow)
            if unresolved:
                questions = [a.clarification for a in unresolved]
                for q in questions:
                    if q.id not in workflow.clarification_map():
                        workflow.upsert_clarification(q)

                if non_interactive:
                    for q in questions:
                        print(f"[non-interactive] [{q.id}] {q.question}")
                    report = BuildReport(
                        stop_reason="needs_input",
                        exit_code=2,
                        iterations=iteration,
                        ask_rounds=ask_budget.rounds_used,
                        remaining_ambiguities=[q.id for q in questions],
                        questions_asked=asked_ids,
                        answers=all_answers,
                        message="Ambiguity in --non-interactive mode",
                    )
                    report.print_report()
                    return report

                if not ask_budget.can_ask():
                    report = BuildReport(
                        stop_reason="ask_budget_exhausted",
                        exit_code=2,
                        iterations=iteration,
                        ask_rounds=ask_budget.rounds_used,
                        remaining_ambiguities=[q.id for q in questions],
                        questions_asked=asked_ids,
                        answers=all_answers,
                        score=last_compare.score if last_compare else 0.0,
                        failing_checks=last_compare.failing_checks() if last_compare else [],
                    )
                    report.print_report()
                    return report

                try:
                    ask_result = ask_user(
                        questions,
                        ask_budget,
                        non_interactive=False,
                        already_answered=set(workflow.clarification_map()),
                    )
                except UserAbort as exc:
                    report = BuildReport(
                        stop_reason="user_abort",
                        exit_code=1,
                        iterations=iteration,
                        ask_rounds=ask_budget.rounds_used,
                        questions_asked=asked_ids,
                        answers=all_answers,
                        message=str(exc),
                    )
                    report.print_report()
                    return report

                if ask_result.skipped_ids and not ask_result.answers:
                    report = BuildReport(
                        stop_reason="ask_budget_exhausted",
                        exit_code=2,
                        iterations=iteration,
                        ask_rounds=ask_budget.rounds_used,
                        remaining_ambiguities=ask_result.skipped_ids,
                        questions_asked=asked_ids,
                        answers=all_answers,
                    )
                    report.print_report()
                    return report

                asked_ids.extend(ask_result.answers.keys())
                all_answers.update(ask_result.answers)
                _apply_answers(workflow, ask_result.answers)

                result = execute_workflow(
                    workflow,
                    input_xlsx=input_xlsx,
                    pdf_path=pdf_path,
                    output_xlsx=tmp_out,
                    collect_ambiguities=True,
                )
                unresolved = _unresolved(result.ambiguities, workflow)

            if result.errors and not result.diu_rows:
                report = BuildReport(
                    stop_reason="execute_error",
                    exit_code=1,
                    iterations=iteration,
                    ask_rounds=ask_budget.rounds_used,
                    questions_asked=asked_ids,
                    answers=all_answers,
                    failing_checks=result.errors,
                    remaining_ambiguities=[a.clarification.id for a in unresolved],
                    message="; ".join(result.errors),
                )
                report.print_report()
                return report

            compare = compare_diu(result.diu_rows, target_rows)
            last_compare = compare
            print(f"[update iter {iteration}/{max_iters}] {compare.summary}")

            if compare.passed:
                path = save_workflow(workflow, out_workflow)
                report = BuildReport(
                    stop_reason="success",
                    exit_code=0,
                    score=compare.score,
                    iterations=iteration,
                    ask_rounds=ask_budget.rounds_used,
                    questions_asked=asked_ids,
                    answers=all_answers,
                    workflow_path=path,
                    message="DIU matches target; updated workflow saved.",
                )
                report.print_report()
                return report

            if compare.score <= prev_score + 1e-12 and iteration > 1:
                report = BuildReport(
                    stop_reason="no_progress",
                    exit_code=1,
                    score=compare.score,
                    iterations=iteration,
                    ask_rounds=ask_budget.rounds_used,
                    questions_asked=asked_ids,
                    answers=all_answers,
                    failing_checks=compare.failing_checks(),
                    remaining_ambiguities=[a.clarification.id for a in unresolved],
                    message=(
                        "Update stalled. Unresolved roles/fields: "
                        + (
                            ", ".join(a.clarification.id for a in unresolved)
                            or "(none)"
                        )
                    ),
                )
                report.print_report()
                return report

            prev_score = compare.score

            if iteration >= max_iters:
                report = BuildReport(
                    stop_reason="max_build_iters",
                    exit_code=1,
                    score=compare.score,
                    iterations=iteration,
                    ask_rounds=ask_budget.rounds_used,
                    questions_asked=asked_ids,
                    answers=all_answers,
                    failing_checks=compare.failing_checks(),
                    remaining_ambiguities=[a.clarification.id for a in unresolved],
                    message=(
                        "Max update iterations reached. Failing checks / unresolved: "
                        + "; ".join(compare.failing_checks()[:10])
                    ),
                )
                path = save_workflow(workflow, out_workflow)
                report.workflow_path = path
                report.print_report()
                return report

            # Bindings-focused patch nudge for the next iteration.
            nudge_checks = list(compare.failing_checks())
            nudge_checks.insert(
                0,
                "Prefer fixing bindings (sheet_hints, header_hints, column_aliases, "
                "resolved_sheet); keep intent/steps/validators; preserve clarifications.",
            )
            workflow = patch_workflow(
                workflow,
                compare.summary,
                nudge_checks,
                summary,
            )
            _apply_answers(workflow, all_answers)

    return BuildReport(stop_reason="max_build_iters", exit_code=1)
