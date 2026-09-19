"""Evidence bundle assembly and artifact writing."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import config as cfg


class EvidenceError(RuntimeError):
    pass


def _timestamp_dir() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


class RunContext:
    def __init__(self, run_label: str | None = None) -> None:
        self.config = cfg.Config()
        label = run_label or _timestamp_dir()
        self.run_dir = cfg.REPO / "demo" / "artifacts" / label
        self.log_dir = self.run_dir / "logs"
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.log_dir.mkdir()
        self.files: list[str] = []

    def write(self, name: str, content: str | bytes) -> Path:
        path = self.run_dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        self.files.append(str(path.relative_to(cfg.REPO)))
        return path

    def save_log(self, name: str, content: str) -> None:
        path = self.log_dir / name
        path.write_text(content, encoding="utf-8")
        self.files.append(str(path.relative_to(cfg.REPO)))

    def scan_for_secrets(self, payload: str) -> None:
        for secret in self.config.secret_values:
            if secret in payload:
                raise EvidenceError(
                    "refusing to write evidence containing a configured secret"
                )

    def finalize(self, evidence: dict[str, Any]) -> Path:
        blob = json.dumps(evidence, indent=2, sort_keys=True)
        redacted = blob
        for secret in self.config.secret_values:
            if secret in redacted:
                redacted = redacted.replace(secret, "[REDACTED]")
        self.scan_for_secrets(redacted)
        payload = json.dumps(json.loads(redacted), indent=2, sort_keys=True)
        return self.write("evidence.json", payload + "\n")

    def summary(self) -> list[str]:
        return self.files


def timeline_text(scenario: dict[str, Any]) -> str:
    steps = scenario.get("timeline") or []
    lines = []
    for index, step in enumerate(steps):
        marker = "STALENESS" if step["stage"] == "STALENESS" else step["stage"]
        value = step.get("value", "")
        detail = f"  {value}" if value else ""
        lines.append(f"{marker}{detail}")
        if index < len(steps) - 1:
            lines.append("↓")
    return "\n".join(lines)


def log_excerpt(log_text: str, needles: list[str], around: int = 0) -> str:
    matches = []
    for line in log_text.splitlines():
        if any(needle in line for needle in needles):
            matches.append(line)
    return "\n".join(matches[:500])


def extract_json_lines(log_text: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON from docker log output."""
    records: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            records.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            continue
    return records


def clean_key_check(document: dict[str, Any]) -> tuple[bool, list[str]]:
    bad = [key for key in document if key.startswith("_Document__")]
    return (not bad, bad)


def truncate(value: Any, limit: int = 2000) -> str:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... (truncated {len(text) - limit} chars)"
