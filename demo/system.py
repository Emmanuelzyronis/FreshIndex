"""Interact with the live FreshIndex stack through its real boundaries.

The harness never fabricates pipeline data. PostgreSQL is reached through psql
inside the postgres container, Redis through redis-cli inside the redis
container, Meilisearch and the monitor through their published HTTP endpoints,
and the reader/indexer through their internal health endpoints (via docker
exec) plus docker compose logs.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import config as cfg


class DemoError(RuntimeError):
    pass


def _run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = 300,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            cwd=cwd or str(cfg.REPO),
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except subprocess.TimeoutExpired as exc:
        raise DemoError(f"command timed out after {timeout}s: {' '.join(argv)}") from exc


def compose(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
    return _run(["docker", "compose", *args], check=check, timeout=timeout)


def compose_out(*args: str, check: bool = True) -> str:
    return compose(*args, check=check).stdout.strip()


def exec_in(
    service: str,
    argv: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
    timeout: float = 120,
) -> subprocess.CompletedProcess:
    base = ["docker", "compose", "exec", "-T"]
    for key, value in (env_extra or {}).items():
        base += ["-e", f"{key}={value}"]
    return _run(
        [*base, service, *argv],
        input_text=input_text,
        check=check,
        timeout=timeout,
    )


def psql(
    sql: str,
    *,
    role: str = "writer",
    check: bool = True,
    timeout: float = 120,
) -> str:
    c = cfg.Config()
    env_extra = {"PGPASSWORD": c.env(f"{role.upper()}_DB_PASSWORD", "")}
    if role == "postgres":
        env_extra = {"PGPASSWORD": c.env("POSTGRES_PASSWORD", "")}
    result = exec_in(
        "postgres",
        [
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            "127.0.0.1",
            "-U",
            c.env(f"{role.upper()}_DB_USER", role),
            "-d",
            c.env("POSTGRES_DB", "catalog"),
        ],
        input_text=sql,
        check=False,
        env_extra=env_extra,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise DemoError(
            f"psql failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:800] or result.stdout.strip()[:800]}"
        )
    return result.stdout


def redis_cli(*args: str, check: bool = True) -> str:
    c = cfg.Config()
    return exec_in(
        "redis",
        ["redis-cli", *args],
        check=check,
        env_extra={"REDISCLI_AUTH": c.env("REDIS_PASSWORD", "")},
    ).stdout


def redis_json(*args: str) -> Any:
    return json.loads(redis_cli("--json", *args))


def stream_state() -> dict[str, Any]:
    info = redis_json("XINFO", "STREAM", "cdc_events")
    stream = info[0] if isinstance(info, list) else info
    return {
        "length": int(stream.get("length") or 0),
        "last_id": stream.get("last-generated-id") or "",
    }


def xpending_count() -> int:
    value = redis_json("XPENDING", "cdc_events", "indexers")
    if isinstance(value, list) and value:
        return int(value[0] or 0)
    return int(value or 0)


def dlq_length() -> int:
    return int(redis_cli("XLEN", "cdc_events_dlq").strip() or 0)


def stream_events_after(start_id: str) -> list[dict[str, Any]]:
    """Return [{id, event_json}, ...] entries strictly after start_id."""
    raw = redis_json("XRANGE", "cdc_events", f"({start_id}", "+")
    entries: list[dict[str, Any]] = []
    for entry in raw or []:
        message_id, field_list = entry[0], entry[1]
        fields = {
            field_list[i]: field_list[i + 1]
            for i in range(0, len(field_list), 2)
        }
        entries.append({"id": message_id, "event": fields.get("event", "")})
    return entries


def meili(method: str, path: str, body: Any = None, check: bool = True) -> Any:
    c = cfg.Config()
    request = urllib.request.Request(
        f"{c.meili_url}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {c.env('MEILI_MASTER_KEY', '')}",
            "Content-Type": "application/json",
        },
    )
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(request, data=data, timeout=10) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        if not check:
            return None
        raise DemoError(
            f"Meilisearch {method} {path} failed: {exc.code} {exc.read()[:400]!r}"
        ) from exc


def meili_document(index: str, document_id: str) -> dict[str, Any] | None:
    return meili("GET", f"/indexes/{index}/documents/{document_id}", check=False)


def meili_search(index: str, body: dict[str, Any]) -> dict[str, Any]:
    return meili("POST", f"/indexes/{index}/search", body)


def monitor(path: str = "/staleness") -> dict[str, Any]:
    c = cfg.Config()
    with urllib.request.urlopen(f"{c.monitor_url}{path}", timeout=5) as response:
        return json.loads(response.read())


def service_ready_via_exec(service: str, port: int, timeout: float = 5) -> bool:
    script = (
        "import urllib.request;"
        f"urllib.request.urlopen('http://127.0.0.1:{port}/ready', timeout={timeout})"
    )
    result = exec_in(service, ["python", "-c", script], check=False)
    return result.returncode == 0


def container_status(service: str) -> str:
    result = compose("ps", "-a", "-q", service, check=False)
    container_id = result.stdout.strip()
    if not container_id:
        return "absent"
    inspect = _run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container_id,
        ],
        check=False,
    )
    return inspect.stdout.strip() or "unknown"


def compose_ps_json() -> list[dict[str, Any]]:
    result = compose("ps", "-a", "--format", "json", check=False)
    services: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if line.strip():
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return services


def service_logs(service: str) -> str:
    result = compose("logs", "--no-color", "--timestamps", service, check=False)
    return result.stdout


def wait_for(
    fn: Callable[[], bool],
    timeout: float,
    interval: float = 1.0,
    description: str = "condition",
) -> None:
    deadline = time.monotonic() + timeout
    last = False
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return
        time.sleep(interval)
    raise DemoError(f"timed out waiting for {description}")


def slot_lag_bytes() -> dict[str, int]:
    sql = (
        "SELECT slot_name, "
        "pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint "
        "FROM pg_replication_slots WHERE slot_name IN "
        "('cdc_products_slot','staleness_monitor_slot') ORDER BY slot_name;"
    )
    out = psql(sql, role="postgres")
    result: dict[str, int] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) == 2:
            result[parts[0].strip()] = int(parts[1].strip())
    return result


def health_snapshot() -> dict[str, Any]:
    services: dict[str, Any] = {}
    for service, port in (
        ("postgres", None),
        ("redis", None),
        ("meilisearch", None),
        ("cdc-reader", 8082),
        ("indexer", 8081),
        ("monitor", 8080),
    ):
        services[service] = {"status": container_status(service)}
    for row in compose_ps_json():
        service = row.get("Service")
        if service in services:
            services[service]["health"] = row.get("Health")
            services[service]["status"] = row.get("Status")
    return services


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
