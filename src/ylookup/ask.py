"""Interactive clarifying questions with hard ask budgets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ylookup.model.dto.workflow import Clarification


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return int(raw)


@dataclass
class AskBudget:
    max_rounds: int = field(default_factory=lambda: env_int("MAX_ASK_ROUNDS", 3))
    max_questions_per_round: int = field(
        default_factory=lambda: env_int("MAX_QUESTIONS_PER_ROUND", 5)
    )
    rounds_used: int = 0
    asked_ids: set[str] = field(default_factory=set)

    @property
    def rounds_remaining(self) -> int:
        return max(0, self.max_rounds - self.rounds_used)

    def can_ask(self) -> bool:
        return self.rounds_remaining > 0


class UserAbort(Exception):
    """Raised when the user aborts an ask round."""


@dataclass
class AskRoundResult:
    answers: dict[str, str]
    aborted: bool = False
    skipped_ids: list[str] = field(default_factory=list)


def filter_new_questions(
    questions: list[Clarification],
    budget: AskBudget,
    already_answered: set[str],
) -> list[Clarification]:
    """Drop repeats and already-answered ids; cap to per-round limit."""
    out: list[Clarification] = []
    for q in questions:
        if q.id in budget.asked_ids or q.id in already_answered:
            continue
        if q.answer is not None and str(q.answer).strip() != "":
            continue
        out.append(q)
        if len(out) >= budget.max_questions_per_round:
            break
    return out


def format_ask_prompt(questions: list[Clarification], round_num: int, max_rounds: int) -> str:
    lines = [
        f"[ask {round_num}/{max_rounds}] Need clarification ({len(questions)} questions):"
    ]
    for i, q in enumerate(questions, start=1):
        opts = ""
        if q.options:
            opts = " " + " / ".join(f"({o})" for o in q.options)
        lines.append(f"  {i}) [{q.id}] {q.question}{opts}")
    lines.append(
        "Enter answers as: id=value (comma-separated), or numbered value1;value2 — "
        "or 'quit' to abort."
    )
    return "\n".join(lines)


def parse_answers(raw: str, questions: list[Clarification]) -> dict[str, str]:
    text = raw.strip()
    if not text:
        raise UserAbort("empty input")
    lower = text.lower()
    if lower in {"quit", "q", "abort", "exit"}:
        raise UserAbort(text)

    answers: dict[str, str] = {}
    # id=value pairs
    if "=" in text and any(q.id in text for q in questions):
        parts = [p.strip() for p in text.split(",") if p.strip()]
        for part in parts:
            if "=" not in part:
                continue
            kid, val = part.split("=", 1)
            answers[kid.strip()] = val.strip()
        return answers

    # Semicolon / pipe separated positional
    seps = [";", "|"]
    chunks = [text]
    for sep in seps:
        if sep in text:
            chunks = [c.strip() for c in text.split(sep)]
            break
    else:
        # Single answer for single question
        if len(questions) == 1:
            return {questions[0].id: text}
        chunks = [c.strip() for c in text.split(",")]

    for q, val in zip(questions, chunks, strict=False):
        answers[q.id] = val
    return answers


def ask_user(
    questions: list[Clarification],
    budget: AskBudget,
    *,
    non_interactive: bool = False,
    already_answered: set[str] | None = None,
) -> AskRoundResult:
    """Batch-ask up to MAX_QUESTIONS_PER_ROUND. Returns answers keyed by id."""
    answered = already_answered or set()
    batch = filter_new_questions(questions, budget, answered)
    if not batch:
        return AskRoundResult(answers={})

    if non_interactive or not budget.can_ask():
        return AskRoundResult(
            answers={},
            aborted=False,
            skipped_ids=[q.id for q in batch],
        )

    round_num = budget.rounds_used + 1
    prompt = format_ask_prompt(batch, round_num, budget.max_rounds)
    print(prompt)
    try:
        raw = input("> ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise UserAbort(str(exc) or "interrupt") from exc

    parsed = parse_answers(raw, batch)
    budget.rounds_used += 1
    for q in batch:
        budget.asked_ids.add(q.id)
    return AskRoundResult(answers=parsed)
