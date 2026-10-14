"""Minimal .env loader (no extra dependency)."""

from __future__ import annotations

from pathlib import Path
import os


def load_dotenv(path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ if not already set."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / ".env")
        # Also try repo root relative to package
        candidates.append(Path(__file__).resolve().parents[2] / ".env")

    for env_path in candidates:
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("'").strip('"')
            if key and key not in os.environ:
                os.environ[key] = val
        break
