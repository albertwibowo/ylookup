"""OpenRouter client for structured workflow propose/patch JSON."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ylookup.model.dto.workflow import WorkflowDTO, default_workflow

DEFAULT_MODEL = "qwen/qwen2.5-32b-instruct"


def get_model() -> str:
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)


def get_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key and key.strip():
        return key.strip()
    return None


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            raise
        return json.loads(m.group(0))


def chat_json(system: str, user: str, *, temperature: float = 0.1) -> dict[str, Any]:
    """Call OpenRouter chat and parse a JSON object from the reply."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    from openrouter import OpenRouter

    client = OpenRouter(api_key=api_key)
    result = client.chat.send(
        model=get_model(),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    # SDK returns ChatResult; content path varies slightly by version.
    content = None
    choices = getattr(result, "choices", None)
    if choices:
        message = choices[0].message
        content = getattr(message, "content", None)
    if content is None and hasattr(result, "model_dump"):
        dumped = result.model_dump()
        try:
            content = dumped["choices"][0]["message"]["content"]
        except Exception:  # noqa: BLE001
            content = None
    if not content:
        raise RuntimeError(f"Empty LLM response: {result!r}")
    if isinstance(content, list):
        # multimodal content blocks
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        content = "".join(parts)
    return _extract_json(str(content))


def propose_workflow(profile_summary: dict[str, Any], context: str | None = None) -> WorkflowDTO:
    """Ask the LLM for an initial workflow; fall back to heuristic default."""
    base = default_workflow()
    if not get_api_key():
        return base

    system = (
        "You are a fund ops workflow designer. Reply with a single JSON object matching "
        "this schema keys: intent, bindings, steps, validators, policies, clarifications, aliases. "
        "steps must be parse_pdf → match_payments → build_diu_rows → write_diu. "
        "policies.on_no_match is leave_unpaid or abort; policies.emit is all_tracker_investors. "
        "Do not hardcode Excel cell addresses like B14; use semantic sheet/header hints. "
        "Sheet names may be renamed; put durable synonyms in sheet_hints and identify "
        "roles via header_hints (column/label text) that remain stable across renames."
    )
    user = {
        "task": "Propose a DIU-fill workflow for capital call bank matching.",
        "profile": profile_summary,
        "context": context or "",
        "seed": base.model_dump(mode="json"),
    }
    try:
        data = chat_json(system, json.dumps(user))
        # Merge onto default so required steps always exist.
        merged = base.model_dump(mode="json")
        for key in ("intent", "bindings", "steps", "validators", "policies", "aliases"):
            if key in data and data[key]:
                merged[key] = data[key]
        return WorkflowDTO.model_validate(merged)
    except Exception:  # noqa: BLE001 — builder continues with heuristic
        return base


def patch_workflow(
    workflow: WorkflowDTO,
    compare_summary: str,
    failing_checks: list[str],
    profile_summary: dict[str, Any],
) -> WorkflowDTO:
    """Ask the LLM to patch policies/bindings/aliases after a failed compare."""
    if not get_api_key():
        return workflow

    system = (
        "You repair DIU workflows. Return a full JSON workflow object. "
        "Prefer fixing policies, aliases, and match thresholds. Keep steps order. "
        "Preserve existing clarifications answers."
    )
    user = {
        "current_workflow": workflow.model_dump(mode="json"),
        "compare_summary": compare_summary,
        "failing_checks": failing_checks[:30],
        "profile": profile_summary,
    }
    try:
        data = chat_json(system, json.dumps(user))
        # Preserve clarifications from current workflow if model drops them
        clar = data.get("clarifications") or [
            c.model_dump(mode="json") for c in workflow.clarifications
        ]
        data["clarifications"] = clar
        return WorkflowDTO.model_validate(data)
    except Exception:  # noqa: BLE001
        return workflow
