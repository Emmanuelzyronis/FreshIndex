#!/usr/bin/env bash
#
# FreshIndex portfolio evidence demonstration — single entry point.
#
# Usage:
#   bash demo/run.sh                 # run all scenarios (A, B, SLO, C)
#   bash demo/run.sh --scenarios a   # run only scenario A
#   bash demo/run.sh --label run-01  # name the artifact directory
#
# Prerequisites: a running FreshIndex stack (docker compose up -d --build),
# docker compose, and python3. Secrets are read from .env and are never
# printed or written into evidence artifacts.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

if [[ ! -f .env ]]; then
  echo "error: .env is required (copy .env.example and set secrets)" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is required" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required" >&2
  exit 1
fi

if ! docker compose ps >/dev/null 2>&1; then
  echo "FreshIndex stack is not running; starting it with docker compose up -d" >&2
  docker compose up -d
fi

exec python3 -m demo.run_demo "$@"
