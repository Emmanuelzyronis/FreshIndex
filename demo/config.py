"""FreshIndex demo configuration.

Reads the repository .env (with real exported environment variables taking
precedence, mirroring docker compose) and exposes both the values the harness
needs to talk to the live system and a strictly safe subset that may be
written into evidence artifacts. Secret values never enter evidence output;
they are only passed through process environments or HTTP headers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]

_SECRET_KEY_PATTERN = re.compile(r"(PASSWORD|MASTER_KEY|SECRET|TOKEN|API_KEY)")


def git_describe(repo: Path = REPO) -> dict[str, str]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    short = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "commit": commit.stdout.strip() if commit.returncode == 0 else "unknown",
        "commit_short": short.stdout.strip() if short.returncode == 0 else "unknown",
        "dirty": bool(dirty.stdout.strip()),
    }


class Config:
    """Typed access to environment plus safe metadata for evidence output."""

    def __init__(self, env_path: Path = REPO / ".env") -> None:
        values: dict[str, str] = {}
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        for key, value in os.environ.items():
            if key in values or re.fullmatch(r"[A-Z0-9_]+", key):
                values[key] = value
        self.values = values
        self.secret_values: list[str] = [
            value
            for key, value in values.items()
            if _SECRET_KEY_PATTERN.search(key) and len(value) >= 8
        ]

    def env(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def port(self, key: str, default: str) -> int:
        try:
            return int(self.env(key, default) or default)
        except ValueError:
            return int(default)

    @property
    def monitor_url(self) -> str:
        return f"http://127.0.0.1:{self.port('MONITOR_PORT', '8080')}"

    @property
    def meili_url(self) -> str:
        return f"http://127.0.0.1:{self.port('MEILI_PORT', '7700')}"

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "project": "FreshIndex",
            "stack": "docker compose",
            "postgres_db": self.env("POSTGRES_DB", "catalog"),
            "writer_user": self.env("WRITER_DB_USER", "catalog_writer"),
            "cdc_user": self.env("CDC_DB_USER", "cdc_reader"),
            "ports": {
                "postgres": self.port("POSTGRES_PORT", "5432"),
                "redis": self.port("REDIS_PORT", "6379"),
                "meilisearch": self.port("MEILI_PORT", "7700"),
                "monitor": self.port("MONITOR_PORT", "8080"),
            },
            "config": {
                "staleness_slo_ms": self.env("STALENESS_SLO_MS", "1000"),
                "staleness_poll_seconds": self.env("STALENESS_POLL_SECONDS", "0.12"),
                "sample_window": self.env("SAMPLE_WINDOW", "10000"),
                "indexer_workers": self.env("INDEXER_WORKERS", "4"),
                "indexer_batch_size": self.env("INDEXER_BATCH_SIZE", "50"),
                "indexer_claim_idle_ms": self.env("CDC_CLAIM_IDLE_MS", "30000"),
                "cdc_max_attempts": self.env("CDC_MAX_ATTEMPTS", "5"),
                "loadgen_rate": self.env("LOADGEN_RATE", "5"),
                "loadgen_duration_seconds": self.env("LOADGEN_DURATION_SECONDS", "60"),
                "loadgen_seed": self.env("LOADGEN_SEED", "2026"),
                "indexer_processing_delay_ms": self.env(
                    "INDEXER_PROCESSING_DELAY_MS", "0"
                ),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.safe_metadata(), sort_keys=True)
