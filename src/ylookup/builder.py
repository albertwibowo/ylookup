"""Build loop: propose → execute → ask → compare → patch, with hard stops."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ylookup.ask import AskBudget, UserAbort, ask_user, env_int
from ylookup.compare.diu import CompareResult, compare_diu
from ylookup.llm import patch_workflow, propose_workflow
from ylookup.model.dao.workflow import save_workflow
from ylookup.model.dto.workflow import Clarification, WorkflowDTO
from ylookup.runner import Ambiguity, execute_workflow, heuristic_propose_workflow
from ylookup.tools.excel import profile_workbook, read_diu_rows


@dataclass
class BuildReport:
    stop_reason: str
    exit_code: int
    score: float = 0.0
    iterations: int = 0
    ask_rounds: int = 0
    questions_asked: list[str] = field(default_factory=list)
    answers: dict[str, str] = field(default_factory=dict)
    remaining_ambiguities: list[str] = field(default_factory=list)
    failing_checks: list[str] = field(default_factory=list)
    workflow_path: Path | None = None
    message: str = ""

    def print_report(self) -> None:
        print("\n=== Build report ===")
        print(f"stop: {self.stop_reason}")
        print(f"score: {self.score:.4f}  iters: {self.iterations}  ask_rounds: {self.ask_rounds}")
        if self.failing_checks:
            print("failing checks:")
            for line in self.failing_checks[:15]:
                print(f"  - {line}")
        if self.questions_asked:
            print(f"questions asked: {', '.join(self.questions_asked)}")
        if self.answers:
            print("answers:")
            for k, v in self.answers.items():
                print(f"  {k} = {v}")
        if self.remaining_ambiguities:
            print(f"remaining ambiguities: {', '.join(self.remaining_ambiguities)}")
        if self.workflow_path:
            print(f"workflow: {self.workflow_path}")
        if self.message:
            print(self.message)


def _profile_summary(input_xlsx: Path) -> dict[str, Any]:
    profile = profile_workbook(input_xlsx)
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
    }


def _apply_answers(workflow: WorkflowDTO, answers: dict[str, str]) -> None:
    for qid, answer in answers.items():
        # Policy shortcuts
        if qid.startswith("policy:") or qid in {"pdf:empty_payments"}:
            if answer in {"leave_unpaid", "abort"}:
                workflow.policies.on_no_match = answer  # type: ignore[assignment]
        if qid.startswith("match:") and answer.lower() not in {"skip", "none", "unmatched"}:
            workflow.aliases[qid] = answer
        if qid.startswith("bind:"):
            role = qid.removeprefix("bind:")
            binding = getattr(workflow.bindings, role, None)
            if binding is not None and answer:
                binding.resolved_sheet = answer
                # Keep the chosen name as a sticky hint for later patches.
                if answer not in binding.sheet_hints:
                    binding.sheet_hints = [answer, *binding.sheet_hints]
        if qid.startswith("col:") and answer:
            parts = qid.split(":", 2)
            if len(parts) == 3:
                _, role, field_name = parts
                binding = getattr(workflow.bindings, role, None)
                if binding is not None:
                    aliases = list(binding.column_aliases.get(field_name) or [])
                    if answer not in aliases:
                        binding.column_aliases[field_name] = [answer, *aliases]
                    if answer not in binding.header_hints:
                        binding.header_hints = [answer, *binding.header_hints]
        # Find existing clarification question text if any
        existing_q = next((c.question for c in workflow.clarifications if c.id == qid), qid)
        workflow.upsert_clarification(
            Clarification(id=qid, question=existing_q, answer=answer)
        )


def _unresolved(ambiguities: list[Ambiguity], workflow: WorkflowDTO) -> list[Ambiguity]:
    answered = set(workflow.clarification_map())
    return [a for a in ambiguities if a.clarification.id not in answered]


def build_workflow(
    *,
    input_xlsx: str | Path,
    pdf_path: str | Path,
    target_xlsx: str | Path,
    out_workflow: str | Path,
    context_path: str | Path | None = None,
    non_interactive: bool = False,
) -> BuildReport:
    input_xlsx = Path(input_xlsx)
    pdf_path = Path(pdf_path)
    target_xlsx = Path(target_xlsx)
    out_workflow = Path(out_workflow)

    max_iters = env_int("MAX_BUILD_ITERS", 3)
    ask_budget = AskBudget()
    context = Path(context_path).read_text(encoding="utf-8") if context_path else None

    summary = _profile_summary(input_xlsx)
    try:
        workflow = propose_workflow(summary, context)
    except Exception:  # noqa: BLE001
        workflow = heuristic_propose_workflow()

    target_rows = read_diu_rows(target_xlsx)
    prev_score = -1.0
    last_compare: CompareResult | None = None
    all_answers: dict[str, str] = {}
    asked_ids: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ylookup-build-") as tmp:
        tmp_out = Path(tmp) / "diu-candidate.xlsx"

        for iteration in range(1, max_iters + 1):
            result = execute_workflow(
                workflow,
                input_xlsx=input_xlsx,
                pdf_path=pdf_path,
                output_xlsx=tmp_out,
                collect_ambiguities=True,
            )
            if result.errors and not result.diu_rows:
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
                # Ensure workflow knows the question text
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

                # Re-execute after answers within same iteration budget slot
                result = execute_workflow(
                    workflow,
                    input_xlsx=input_xlsx,
                    pdf_path=pdf_path,
                    output_xlsx=tmp_out,
                    collect_ambiguities=True,
                )
                unresolved = _unresolved(result.ambiguities, workflow)
                if unresolved:
                    # More questions may remain; loop will ask next iteration if budget allows
                    # But compare anyway to detect progress.
                    pass

            compare = compare_diu(result.diu_rows, target_rows)
            last_compare = compare
            print(f"[build iter {iteration}/{max_iters}] {compare.summary}")

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
                    message="DIU matches target; workflow saved.",
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
                )
                # Still save best-effort workflow for inspection
                path = save_workflow(workflow, out_workflow)
                report.workflow_path = path
                report.print_report()
                return report

            # Patch for next iteration
            workflow = patch_workflow(
                workflow,
                compare.summary,
                compare.failing_checks(),
                summary,
            )
            # Re-apply sticky answers after patch
            _apply_answers(workflow, all_answers)

    return BuildReport(stop_reason="max_build_iters", exit_code=1)
