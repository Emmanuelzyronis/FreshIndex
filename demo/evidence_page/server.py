"""Read-only evidence proxy for the static FreshIndex evidence page.

Serves the static page and forwards its requests to the real system
boundaries (monitor HTTP endpoints and Meilisearch) and to lightweight docker
exec probes for Redis pending/DLQ and indexer queue state. No values are
mocked and no secrets are exposed; the proxy binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = Path(__file__).resolve().parent / "index.html"


def latest_evidence() -> dict[str, Any]:
    artifacts = REPO / "demo" / "artifacts"
    candidates = sorted(path for path in artifacts.glob("*/evidence.json") if path.is_file())
    if not candidates:
        return {"error": "no evidence bundle available"}
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = REPO / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


class Cache:
    def __init__(self, ttl: float = 1.5) -> None:
        self.ttl = ttl
        self.values: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, producer) -> Any:
        now = time.monotonic()
        cached = self.values.get(key)
        if cached and now - cached[0] < self.ttl:
            return cached[1]
        value = producer()
        self.values[key] = (now, value)
        return value


class Handler(BaseHTTPRequestHandler):
    env = load_env()
    cache = Cache()

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _fetch(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def _compose(self, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            ["docker", "compose", *args],
            cwd=str(REPO),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.stdout

    def _redis(self, *args: str) -> str:
        return self._compose(
            "exec",
            "-T",
            "-e",
            f"REDISCLI_AUTH={self.env.get('REDIS_PASSWORD', '')}",
            "redis",
            "redis-cli",
            *args,
        )

    def _monitor_url(self) -> str:
        return f"http://127.0.0.1:{self.env.get('MONITOR_PORT', '8080')}"

    def _meili_url(self) -> str:
        return f"http://127.0.0.1:{self.env.get('MEILI_PORT', '7700')}"

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            body = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/api/"):
            try:
                self._route_api(self.path)
            except Exception as exc:
                self._json(502, {"error": str(exc)[:200]})
            return
        self._json(404, {"error": "not found"})

    def _route_api(self, path: str) -> None:
        monitor_paths = {
            "/api/staleness": "/staleness",
            "/api/ready": "/ready",
            "/api/health": "/health",
            "/api/metrics": "/metrics",
        }
        if path in monitor_paths:
            data = self._fetch(self._monitor_url() + monitor_paths[path])
            self._json(200, data)
            return
        if path == "/api/evidence":
            self._json(200, latest_evidence())
            return
        if path == "/api/pending":
            raw = self._redis("XPENDING", "cdc_events", "indexers")
            match = re.search(r"\(integer\) (\d+)|\b(\d+)\b", raw or "")
            count = int((match.group(1) or match.group(2)) if match else 0)
            self._json(200, {"pending": count})
            return
        if path == "/api/dlq":
            raw = self._redis("XLEN", "cdc_events_dlq") or ""
            self._json(200, {"dlq": int(raw.strip() or 0)})
            return
        if path == "/api/indexer":
            script = (
                "import json,urllib.request;"
                "print(urllib.request.urlopen("
                "'http://127.0.0.1:8081/health',timeout=3).read().decode())"
            )
            out = self._compose("exec", "-T", "indexer", "python", "-c", script)
            self._json(200, json.loads(out.strip()))
            return
        if path.startswith("/api/search"):
            headers = {
                "Authorization": f"Bearer {self.env.get('MEILI_MASTER_KEY', '')}",
                "Content-Type": "application/json",
            }
            query = {"q": "", "filter": "_deleted = false", "limit": 1}
            request = urllib.request.Request(
                f"{self._meili_url()}/indexes/products/search",
                data=json.dumps(query).encode(),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self._json(200, json.loads(response.read()))
            return
        self._json(404, {"error": "unknown api path"})

    def log_message(self, *_: Any) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8777)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"evidence page: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
