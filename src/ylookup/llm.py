"""OpenRouter client for structured workflow propose/patch JSON."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from pydantic import ValidationError

from ylookup.model.dto.workflow import WorkflowDTO, default_workflow

DEFAULT_MODEL = "qwen/qwen3-235b-a22b-thinking-2507"
DEFAULT_REASONING_EFFORT = "medium"
MAX_VALIDATION_ATTEMPTS = 3


def get_model() -> str:
    return os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)


def get_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key and key.strip():
        return key.strip()
    return None


def get_reasoning_effort() -> str:
    """OpenRouter reasoning.effort (max/xhigh/high/medium/low/minimal/none)."""
    raw = os.environ.get("OPENROUTER_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)
    return (raw or DEFAULT_REASONING_EFFORT).strip().lower()


def _workflow_response_format() -> dict[str, Any]:
    """OpenRouter structured-output format derived from WorkflowDTO."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "workflow_dto",
            "description": "DIU capital-call workflow document",
            # Free-form dict fields (aliases, column_aliases, params, metadata) are not
            # compatible with OpenAI-style strict schemas, so keep strict off.
            "strict": False,
            "schema": WorkflowDTO.model_json_schema(),
        },
    }


def _parse_structured_json(text: str) -> dict[str, Any]:
    """Decode schema-constrained model content (already JSON text from the API)."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object, got {type(data).__name__}")
    return data


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                parts.append(str(getattr(block, "text") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(value)


def _reasoning_from_details(details: Any) -> str:
    if not details:
        return ""
    parts: list[str] = []
    for item in details:
        if isinstance(item, dict):
            text = item.get("text") or item.get("summary")
            if text:
                parts.append(_as_text(text))
            continue
        text = getattr(item, "text", None)
        if text:
            parts.append(_as_text(text))
            continue
        summary = getattr(item, "summary", None)
        if summary:
            parts.append(_as_text(summary))
    return "".join(parts)


def _message_from_result(result: Any) -> Any | None:
    choices = getattr(result, "choices", None)
    if choices:
        return choices[0].message
    if hasattr(result, "model_dump"):
        dumped = result.model_dump()
        try:
            return dumped["choices"][0]["message"]
        except Exception:  # noqa: BLE001
            return None
    return None


def _message_content(result: Any) -> str:
    """Pull text content from an OpenRouter ChatResult."""
    message = _message_from_result(result)
    content = None
    if message is not None:
        if isinstance(message, dict):
            content = message.get("content")
        else:
            content = getattr(message, "content", None)
    if not content:
        raise RuntimeError(f"Empty LLM response: {result!r}")
    return _as_text(content)


def _message_reasoning(result: Any) -> str:
    message = _message_from_result(result)
    if message is None:
        return ""
    if isinstance(message, dict):
        reasoning = _as_text(message.get("reasoning"))
        if reasoning.strip():
            return reasoning
        return _reasoning_from_details(message.get("reasoning_details"))
    reasoning = _as_text(getattr(message, "reasoning", None))
    if reasoning.strip():
        return reasoning
    return _reasoning_from_details(getattr(message, "reasoning_details", None))


def _emit_reasoning(phase: str, text: str, *, live: bool = False) -> None:
    """Print reasoning tokens to stderr so they stay visible beside CLI reports."""
    body = text.strip()
    if not body:
        model = get_model()
        print(
            f"[llm:{phase}] (no reasoning tokens from {model}; "
            "use a thinking model e.g. qwen/qwen3-235b-a22b-thinking-2507)",
            file=sys.stderr,
            flush=True,
        )
        return
    if not live:
        print(f"\n── LLM reasoning ({phase}) ──", file=sys.stderr, flush=True)
        print(body, file=sys.stderr, flush=True)
        print("── end reasoning ──\n", file=sys.stderr, flush=True)


def _delta_field(delta: Any, name: str) -> Any:
    if delta is None:
        return None
    if isinstance(delta, dict):
        return delta.get(name)
    return getattr(delta, name, None)


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.1,
    phase: str = "llm",
) -> dict[str, Any]:
    """Call OpenRouter with WorkflowDTO JSON Schema; stream reasoning to the terminal."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    from openrouter import OpenRouter

    client = OpenRouter(api_key=api_key)
    effort = get_reasoning_effort()
    reasoning_cfg: dict[str, Any] | None = None
    if effort and effort != "none":
        reasoning_cfg = {"effort": effort}

    common: dict[str, Any] = {
        "model": get_model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "response_format": _workflow_response_format(),
    }
    if reasoning_cfg is not None:
        common["reasoning"] = reasoning_cfg

    # Prefer streaming so users see reasoning tokens as they arrive.
    try:
        stream = client.chat.send(**common, stream=True)
    except Exception:  # noqa: BLE001
        stream = None

    if stream is not None:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_open = False
        with stream:
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                rpiece = _as_text(_delta_field(delta, "reasoning"))
                if not rpiece:
                    rpiece = _reasoning_from_details(
                        _delta_field(delta, "reasoning_details")
                    )
                if rpiece:
                    if not reasoning_open:
                        print(
                            f"\n── LLM reasoning ({phase}) ──",
                            file=sys.stderr,
                            flush=True,
                        )
                        reasoning_open = True
                    print(rpiece, end="", file=sys.stderr, flush=True)
                    reasoning_parts.append(rpiece)
                cpiece = _as_text(_delta_field(delta, "content"))
                if cpiece:
                    content_parts.append(cpiece)

        if reasoning_open:
            print("\n── end reasoning ──\n", file=sys.stderr, flush=True)

        content = "".join(content_parts).strip()
        if content:
            if not reasoning_parts:
                _emit_reasoning(phase, "")
            return _parse_structured_json(content)
        # Stream produced no content; fall through to a non-stream request.

    result = client.chat.send(**common, stream=False)
    reasoning = _message_reasoning(result)
    _emit_reasoning(phase, reasoning)
    return _parse_structured_json(_message_content(result))


def _validate_with_retries(
    build: Callable[[str | None], WorkflowDTO],
    *,
    fallback: WorkflowDTO,
    max_attempts: int = MAX_VALIDATION_ATTEMPTS,
) -> WorkflowDTO:
    """Run build() up to max_attempts; pass prior ValidationError/parse error as feedback."""
    feedback: str | None = None
    for _ in range(max_attempts):
        try:
            return build(feedback)
        except (ValidationError, json.JSONDecodeError, RuntimeError, TypeError, ValueError) as exc:
            feedback = str(exc)
        except Exception as exc:  # noqa: BLE001 — treat unexpected LLM failures as retryable
            feedback = str(exc)
    return fallback


def propose_workflow(profile_summary: dict[str, Any], context: str | None = None) -> WorkflowDTO:
    """Ask the LLM for an initial workflow; fall back to heuristic default."""
    base = default_workflow()
    if not get_api_key():
        return base

    system = (
        "You are a fund ops workflow designer. Reply with a single JSON object matching "
        "the provided workflow_dto schema. "
        "steps must be parse_pdf → match_payments → build_diu_rows → write_diu. "
        "policies.on_no_match is leave_unpaid or abort; policies.emit is all_tracker_investors. "
        "Do not hardcode Excel cell addresses like B14; use semantic sheet/header hints. "
        "Sheet names may be renamed; put durable synonyms in sheet_hints and identify "
        "roles via header_hints (column/label text) that remain stable across renames."
    )

    def build(feedback: str | None) -> WorkflowDTO:
        user: dict[str, Any] = {
            "task": "Propose a DIU-fill workflow for capital call bank matching.",
            "profile": profile_summary,
            "context": context or "",
            "seed": base.model_dump(mode="json"),
        }
        if feedback:
            user["validation_error"] = feedback
            user["instruction"] = (
                "Previous reply failed validation. Return a corrected full workflow JSON."
            )
        data = chat_json(system, json.dumps(user), phase="propose")
        # Merge onto default so required steps always exist.
        merged = base.model_dump(mode="json")
        for key in ("intent", "bindings", "steps", "validators", "policies", "aliases"):
            if key in data and data[key]:
                merged[key] = data[key]
        return WorkflowDTO.model_validate(merged)

    return _validate_with_retries(build, fallback=base)


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
        "You repair DIU workflows. Return a full JSON workflow object matching the "
        "workflow_dto schema. Prefer fixing policies, aliases, and match thresholds. "
        "Keep steps order. Preserve existing clarifications answers."
    )

    def build(feedback: str | None) -> WorkflowDTO:
        user: dict[str, Any] = {
            "current_workflow": workflow.model_dump(mode="json"),
            "compare_summary": compare_summary,
            "failing_checks": failing_checks[:30],
            "profile": profile_summary,
        }
        if feedback:
            user["validation_error"] = feedback
            user["instruction"] = (
                "Previous reply failed validation. Return a corrected full workflow JSON."
            )
        data = chat_json(system, json.dumps(user), phase="patch")
        # Preserve clarifications from current workflow if model drops them
        clar = data.get("clarifications") or [
            c.model_dump(mode="json") for c in workflow.clarifications
        ]
        data["clarifications"] = clar
        return WorkflowDTO.model_validate(data)

    # Keep current workflow if repairs never validate (do not wipe to default).
    return _validate_with_retries(build, fallback=workflow)


def adapt_workflow(
    workflow: WorkflowDTO,
    profile_summary: dict[str, Any],
    context: str | None = None,
) -> WorkflowDTO:
    """Remap bindings (sheets/columns) for a perturbed workbook; keep intent/steps/validators."""
    if not get_api_key():
        return workflow

    system = (
        "You adapt an existing DIU workflow to a renamed/reordered workbook. "
        "Return a full JSON workflow object matching the workflow_dto schema. "
        "Update bindings only: sheet_hints, header_hints, column_aliases, resolved_sheet. "
        "Extend synonym lists rather than replacing them when possible. "
        "Never invent Excel cell addresses. "
        "Keep intent, steps, and validators unchanged. "
        "Do not wipe clarifications; preserve prior answers. "
        "Policies may stay as-is unless clearly wrong."
    )

    def build(feedback: str | None) -> WorkflowDTO:
        user: dict[str, Any] = {
            "task": "Adapt workflow bindings to the profiled workbook.",
            "current_workflow": workflow.model_dump(mode="json"),
            "profile": profile_summary,
            "context": context or "",
        }
        if feedback:
            user["validation_error"] = feedback
            user["instruction"] = (
                "Previous reply failed validation. Return a corrected full workflow JSON."
            )
        data = chat_json(system, json.dumps(user), phase="adapt")
        # Freeze structural fields; only take bindings (+ optional policies/aliases).
        merged = workflow.model_dump(mode="json")
        if data.get("bindings"):
            merged["bindings"] = data["bindings"]
        if data.get("policies"):
            merged["policies"] = data["policies"]
        if data.get("aliases"):
            # Merge aliases; prefer existing sticky answers, then model additions.
            merged["aliases"] = {**data["aliases"], **workflow.aliases}
        # Always re-attach clarifications if the model omits or empties them.
        clar = data.get("clarifications")
        if clar:
            # Upsert: keep prior answers when model drops answer text.
            by_id = {c["id"]: c for c in clar if isinstance(c, dict) and c.get("id")}
            for existing in workflow.clarifications:
                dump = existing.model_dump(mode="json")
                incoming = by_id.get(existing.id)
                if incoming is None:
                    by_id[existing.id] = dump
                elif not (incoming.get("answer") or "").strip() and dump.get("answer"):
                    incoming["answer"] = dump["answer"]
            merged["clarifications"] = list(by_id.values())
        else:
            merged["clarifications"] = [
                c.model_dump(mode="json") for c in workflow.clarifications
            ]
        # Force frozen fields
        merged["intent"] = workflow.intent
        merged["steps"] = [s.model_dump(mode="json") for s in workflow.steps]
        merged["validators"] = workflow.validators.model_dump(mode="json")
        return WorkflowDTO.model_validate(merged)

    # Keep current workflow if adapt never validates (do not wipe to default).
    return _validate_with_retries(build, fallback=workflow)
