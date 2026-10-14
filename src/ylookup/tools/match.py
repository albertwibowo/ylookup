"""Fuzzy investor matching against bank deposit descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ylookup.tools.pdf_bank import BankDeposit

_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def normalize_name(text: str) -> str:
    text = text.lower().strip()
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _tokens(text: str) -> list[str]:
    return [t for t in normalize_name(text).split(" ") if t]


def name_similarity(a: str, b: str) -> float:
    """Score overlap between a bank description fragment and an investor name."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    # Compact forms help with truncated wire names (AARON PAT vs Aaron Patel).
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    ratio = SequenceMatcher(None, ca, cb).ratio()

    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return ratio
    # Token containment: bank often has first name + truncated last name.
    contained = 0
    for short in ta:
        if any(long.startswith(short) or short.startswith(long[: max(3, len(short))]) for long in tb):
            contained += 1
    token_score = contained / max(len(tb), 1)
    # Also reward if investor tokens appear as prefixes inside the description blob.
    prefix_hits = sum(
        1 for t in tb if any(tok.startswith(t[: max(3, min(len(t), 5))]) for tok in ta)
    )
    prefix_score = prefix_hits / max(len(tb), 1)
    return max(ratio, 0.55 * token_score + 0.45 * prefix_score, prefix_score)


def extract_payer_hint(description: str) -> str:
    """Best-effort human name hint from a wire description."""
    # Prefer uppercase name tokens immediately after RDOx / RD0x markers.
    m = re.search(
        r"(?:RDO[A-Z0-9]*|RD0[A-Z0-9]*)\s+([A-Z]{2,}(?:\s+[A-Z]{2,}){0,3})",
        description,
    )
    if m:
        return _WS.sub(" ", m.group(1)).strip()
    # Wire descriptions sometimes soft-wrap mid-name (DANIEL BRO + OOKS).
    m2 = re.search(
        r"(?:RDO[A-Z0-9]*|RD0[A-Z0-9]*)\s+([A-Z]{2,}(?:\s+[A-Z]{2,}){0,2})"
        r".{0,40}?([A-Z]{2,})\|",
        description,
    )
    if m2:
        return _WS.sub(" ", f"{m2.group(1)} {m2.group(2)}").strip()
    runs = re.findall(r"\b([A-Z]{2,}(?:\s+[A-Z]{2,}){0,3})\b", description)
    bankish = {"CHASE", "BANK", "WELLS", "FARGO", "PDF", "EUR", "USD", "TT"}
    filtered = [
        r
        for r in runs
        if not all(tok in bankish for tok in r.split())
    ]
    if filtered:
        return max(filtered, key=len)
    return description[:80]


@dataclass(frozen=True)
class MatchCandidate:
    investor_name: str
    investor_id: str | None
    score: float


@dataclass
class PaymentMatch:
    deposit: BankDeposit
    investor_name: str | None
    investor_id: str | None
    score: float
    ambiguous: bool
    candidates: list[MatchCandidate]
    clarification_id: str


def _clarification_id(hint: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", hint.upper()).strip("_")
    return f"match:{slug or 'UNKNOWN'}"


def match_deposits_to_investors(
    deposits: list[BankDeposit],
    investors: list[dict],
    *,
    min_score: float = 0.55,
    ambiguity_margin: float = 0.08,
    aliases: dict[str, str] | None = None,
    clarifications: dict[str, str] | None = None,
) -> list[PaymentMatch]:
    """Match each deposit to at most one investor.

    ``investors`` items need at least ``name``; optional ``id``.
    ``aliases`` maps clarification id or bank hint → investor name.
    ``clarifications`` maps question id → answered investor name (or skip).
    """
    aliases = aliases or {}
    clarifications = clarifications or {}
    results: list[PaymentMatch] = []

    for dep in deposits:
        hint = extract_payer_hint(dep.description)
        qid = _clarification_id(hint)

        forced = clarifications.get(qid) or aliases.get(qid) or aliases.get(
            normalize_name(hint)
        )
        if forced and forced.lower() not in {"skip", "none", "unmatched", ""}:
            results.append(
                PaymentMatch(
                    deposit=dep,
                    investor_name=forced,
                    investor_id=_lookup_id(investors, forced),
                    score=1.0,
                    ambiguous=False,
                    candidates=[],
                    clarification_id=qid,
                )
            )
            continue

        scored: list[MatchCandidate] = []
        for inv in investors:
            name = str(inv["name"])
            score = name_similarity(hint, name)
            # Also score full description lightly.
            score = max(score, 0.85 * name_similarity(dep.description, name))
            scored.append(
                MatchCandidate(
                    investor_name=name,
                    investor_id=inv.get("id"),
                    score=score,
                )
            )
        scored.sort(key=lambda c: c.score, reverse=True)

        best = scored[0] if scored else None
        second = scored[1] if len(scored) > 1 else None
        ambiguous = False
        chosen_name = None
        chosen_id = None
        chosen_score = 0.0

        if best and best.score >= min_score:
            if second and (best.score - second.score) < ambiguity_margin:
                ambiguous = True
            else:
                chosen_name = best.investor_name
                chosen_id = best.investor_id
                chosen_score = best.score
        elif best:
            # Below threshold → treat as unmatched unless clarified later.
            ambiguous = best.score >= (min_score - 0.15) and bool(second)

        results.append(
            PaymentMatch(
                deposit=dep,
                investor_name=chosen_name,
                investor_id=chosen_id,
                score=chosen_score if chosen_name else (best.score if best else 0.0),
                ambiguous=ambiguous,
                candidates=scored[:5],
                clarification_id=qid,
            )
        )
    return results


def _lookup_id(investors: list[dict], name: str) -> str | None:
    target = normalize_name(name)
    for inv in investors:
        if normalize_name(str(inv["name"])) == target:
            return inv.get("id")
    return None


def payments_by_investor(matches: list[PaymentMatch]) -> dict[str, PaymentMatch]:
    """Map investor name → first successful payment match."""
    out: dict[str, PaymentMatch] = {}
    for m in matches:
        if m.investor_name and not m.ambiguous:
            out.setdefault(m.investor_name, m)
    return out
