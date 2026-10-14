"""Persistence helpers for workflow JSON artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from ylookup.model.dto.workflow import WorkflowDTO


def load_workflow(path: str | Path) -> WorkflowDTO:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return WorkflowDTO.model_validate(data)


def save_workflow(workflow: WorkflowDTO, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = workflow.model_dump(mode="json")
    out.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out
