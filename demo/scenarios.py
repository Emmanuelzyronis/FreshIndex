"""Automated demo scenarios against the real FreshIndex stack.

Scenario A: happy path, one deterministic product mutation end to end.
Scenario B: ordering, a committed burst against one product.
Scenario C: failure and recovery by stopping the indexer mid-workload.
SLO: the repository verifier against the real monitor endpoint.

All timestamps, LSNs, latencies, counts, and verdicts come from the live
system (PostgreSQL, Redis Streams, indexer/monitor logs, Meilisearch).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config as cfg
from . import evidence as ev
from . import system as sysio
from .system import DemoError


def us_to_iso(us: int) -> str:
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).isoformat()


def now_us() -> int:
    return time.time_ns() // 1_000


def lsn_value(lsn: str) -> int:
    high, low = lsn.split("/", 1)
    return (int(high, 16) << 32) | int(low, 16)


def _http_ready(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def monitor_ready(timeout: float = 120) -> None:
    sysio.wait_for(
        lambda: _http_ready(f"{cfg.Config().monitor_url}/ready"),
        timeout,
        1.0,
        "monitor readiness",
    )


def wait_quiescent(timeout: float = 120) -> dict[str, Any]:
    """Wait until nothing is in flight and the indexer has drained."""
    def drained() -> bool:
        metrics = sysio.monitor("/staleness")
        if metrics.get("in_flight_count", 0) != 0:
            return False
        if sysio.xpending_count() != 0:
            return False
        return metrics.get("replication_ready") and metrics.get("probe_ready")

    sysio.wait_for(drained, timeout, 1.0, "pipeline quiescence")
    return sysio.monitor("/staleness")


def fresh_monitor_epoch() -> None:
    """Restart the monitor so its in-memory window reflects this demo run.

    Monitor metrics are in-memory by design and reset on restart. A clean
    measurement epoch is required so SLO numbers are attributable to this
    run rather than to earlier ones. Quiescence is confirmed first so the
    monitor's replication slot has flushed and nothing is replayed.
    """
    wait_quiescent()
    time.sleep(3)
    sysio.compose("restart", "monitor")
    monitor_ready()
    time.sleep(2)


def insert_product(sku: str, name: str, description: str, price_cents: int) -> int:
    sql = (
        f"INSERT INTO products (sku, name, description, category, price_cents, in_stock) "
        f"VALUES ('{sku}', '{name}', '{description}', 'demo', {price_cents}, true) "
        f"RETURNING id;"
    )
    out = sysio.psql(sql)
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines:
        raise DemoError(f"insert did not return a product id for sku={sku}")
    return int(lines[0])


def _stream_events_after(start_id: str, timeout: float = 30) -> list[dict[str, Any]]:
    entries = sysio.stream_events_after(start_id)
    deadline = time.monotonic() + timeout
    while not entries and time.monotonic() < deadline:
        time.sleep(0.3)
        entries = sysio.stream_events_after(start_id)
    return entries


def _decode_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decoded = []
    for entry in entries:
        try:
            envelope = json.loads(entry["event"])
        except json.JSONDecodeError:
            continue
        decoded.append({"stream_id": entry["id"], **envelope})
    return decoded


def _events_for_product(entries: list[dict[str, Any]], product_id: int) -> list[dict[str, Any]]:
    return [
        event
        for event in entries
        if str(event.get("pk", {}).get("id")) == str(product_id)
    ]


def _indexer_log_records() -> list[dict[str, Any]]:
    return ev.extract_json_lines(sysio.service_logs("indexer"))


def _monitor_log_records() -> list[dict[str, Any]]:
    return ev.extract_json_lines(sysio.service_logs("monitor"))


def _staleness_for_pk(records: list[dict[str, Any]], product_id: int) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("event") == "staleness_sample"
        and str(record.get("pk")) == str(product_id)
    ]


def _violations_for_pk(records: list[dict[str, Any]], product_id: int) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("event") == "VIOLATION"
        and str(record.get("pk")) == str(product_id)
    ]


def _markers_for_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fetch cdc_visibility markers for the given events from Meilisearch."""
    if not events:
        return []
    commit_us = [int(event["commit_ts_us"]) for event in events]
    lo, hi = min(commit_us), max(commit_us)
    search = sysio.meili_search(
        "cdc_visibility",
        {
            "q": "",
            "filter": f"commit_ts_us >= {lo} AND commit_ts_us <= {hi}",
            "limit": 1000,
        },
    )
    wanted = {event["event_id"] for event in events}
    return [
        hit
        for hit in search.get("hits", [])
        if hit.get("event_id") in wanted
    ]


def _clean_product_document(product_id: int) -> dict[str, Any] | None:
    return sysio.meili_document("products", str(product_id))


def scenario_a(sequence: int) -> dict[str, Any]:
    started = now_us()
    cfg_obj = cfg.Config()
    slo_ms = float(cfg_obj.env("STALENESS_SLO_MS", "1000"))
    metrics_before = sysio.monitor("/staleness")
    stream_before = sysio.stream_state()
    pending_before = sysio.xpending_count()
    dlq_before = sysio.dlq_length()
    samples_before = metrics_before.get("sample_count", 0)

    sku = f"demo-happy-{int(time.time() * 1000)}-{sequence}"
    sql = (
        "BEGIN;\n"
        "INSERT INTO products (sku, name, description, category, price_cents, in_stock) "
        f"VALUES ('{sku}', 'Demo happy path {sequence}', "
        f"'single deterministic committed write', 'demo', {100 + sequence}, true) "
        "RETURNING id AS product_id;\n"
        "SELECT (txid_current() & 4294967295)::text AS xid;\n"
        "COMMIT;\n"
    )
    out = sysio.psql(sql)
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) < 2:
        raise DemoError(f"expected id and xid from commit, got: {out!r}")
    product_id = int(lines[0])
    xid = lines[1]
    pg_after = sysio.psql(
        "SELECT pg_xact_commit_timestamp('%s'::xid)::text; SELECT pg_current_wal_lsn();"
        % xid
    )
    pg_lines = [line for line in pg_after.splitlines() if line.strip()]
    pg_commit_ts_iso = pg_lines[0] if pg_lines else ""
    pg_wal_lsn = pg_lines[1] if len(pg_lines) > 1 else ""

    entries = _stream_events_after(stream_before["last_id"], timeout=20)
    events = [
        event
        for event in _decode_entries(entries)
        if str(event.get("pk", {}).get("id")) == str(product_id)
    ]
    if not events:
        raise DemoError(f"no CDC event observed for product {product_id}")
    event = events[0]
    event_id = event["event_id"]
    commit_lsn = event["commit_lsn"]
    commit_ts_us = int(event["commit_ts_us"])

    def product_visible() -> bool:
        document = _clean_product_document(product_id)
        return bool(
            document
            and document.get("_lsn") == commit_lsn
            and document.get("_deleted") is False
        )

    sysio.wait_for(product_visible, 30, 0.3, "product document in Meilisearch")
    product_doc = _clean_product_document(product_id)
    assert product_doc is not None
    clean, bad_keys = ev.clean_key_check(product_doc)

    def marker_visible() -> bool:
        marker = sysio.meili_document("cdc_visibility", event_id)
        return bool(
            marker
            and marker.get("event_id") == event_id
            and marker.get("commit_lsn") == commit_lsn
        )

    sysio.wait_for(marker_visible, 30, 0.3, "visibility marker")
    marker = sysio.meili_document("cdc_visibility", event_id)

    def sampled() -> bool:
        return sysio.monitor("/staleness").get("sample_count", 0) > samples_before

    sysio.wait_for(sampled, 30, 0.5, "monitor staleness sample")
    monitor_records = _monitor_log_records()
    staleness_records = _staleness_for_pk(monitor_records, product_id)
    violations = _violations_for_pk(monitor_records, product_id)
    if not staleness_records:
        raise DemoError(f"monitor never recorded staleness for product {product_id}")
    staleness_ms = staleness_records[0].get("staleness_ms")
    metrics_after = sysio.monitor("/staleness")

    indexer_records = _indexer_log_records()
    indexer_applies = [
        record
        for record in indexer_records
        if record.get("event") == "indexer_event"
        and str(record.get("document_id")) == str(product_id)
        and record.get("commit_lsn") == commit_lsn
    ]
    indexer_processing_ms = indexer_applies[0].get("batch_processing_ms") if indexer_applies else None
    stream_queue_latency_ms = (
        indexer_applies[0].get("stream_queue_latency_ms") if indexer_applies else None
    )

    published_us = int(event.get("published_ts_us", 0))
    timeline = [
        {"stage": "POSTGRES COMMIT", "value": f"commit_ts {us_to_iso(commit_ts_us)} lsn {commit_lsn}"},
        {
            "stage": "CDC EVENT",
            "value": (
                f"event_id {event_id[:12]}… xid {event.get('xid')} "
                f"pg_commit_ts_confirmed {pg_commit_ts_iso}"
            ),
        },
        {
            "stage": "REDIS STREAM",
            "value": (
                f"stream {stream_before['length']} → {sysio.stream_state()['length']} "
                f"message {event.get('stream_id')} commit→xadd "
                f"{round((published_us - commit_ts_us) / 1000, 3)}ms"
            ),
        },
        {
            "stage": "INDEXER",
            "value": (
                f"result={marker.get('result') if marker else '?'} "
                f"queue_latency {stream_queue_latency_ms}ms "
                f"batch_processing {indexer_processing_ms}ms"
            ),
        },
        {"stage": "MEILISEARCH", "value": f"product id={product_id} _lsn={product_doc.get('_lsn')}"},
        {"stage": "VISIBLE", "value": f"marker {event_id[:12]}… result={marker.get('result') if marker else '?'}"},
        {"stage": "STALENESS", "value": f"{staleness_ms}ms (monitor read-probe, SLO {slo_ms}ms)"},
    ]

    result: dict[str, Any] = {
        "name": "A",
        "title": "Happy path",
        "status": "PASS" if not violations and staleness_ms is not None else "FAIL",
        "product_id": product_id,
        "sku": sku,
        "event_id": event_id,
        "commit_lsn": commit_lsn,
        "commit_ts_us": commit_ts_us,
        "pg_commit_timestamp_iso": pg_commit_ts_iso,
        "pg_wal_lsn_after_commit": pg_wal_lsn,
        "stream_message_id": event.get("stream_id"),
        "published_ts_us": published_us,
        "commit_to_xadd_ms": round((published_us - commit_ts_us) / 1000, 3) if published_us else None,
        "indexer_stream_queue_latency_ms": stream_queue_latency_ms,
        "indexer_batch_processing_ms": indexer_processing_ms,
        "staleness_ms": staleness_ms,
        "slo_ms": slo_ms,
        "violations": [v.get("age_ms") for v in violations],
        "document": product_doc,
        "marker": marker,
        "document_clean": clean,
        "leaked_sdk_keys": bad_keys,
        "monitor_before_sample_count": samples_before,
        "monitor_after_sample_count": metrics_after.get("sample_count"),
        "pending_before": pending_before,
        "dlq_before": dlq_before,
        "timeline": timeline,
    }
    passed = (
        not violations
        and staleness_ms is not None
        and clean
        and marker is not None
        and marker.get("result") == "applied"
    )
    if staleness_ms is not None and staleness_ms > slo_ms:
        passed = False
    result["status"] = "PASS" if passed else "FAIL"
    result["elapsed_seconds"] = round((now_us() - started) / 1_000_000, 1)
    return result


def scenario_b(sequence: int, burst_size: int = 150) -> dict[str, Any]:
    started = now_us()
    metrics_before = sysio.monitor("/staleness")
    samples_before = metrics_before.get("sample_count", 0)
    stream_before = sysio.stream_state()

    sku = f"demo-order-{int(time.time() * 1000)}-{sequence}"
    product_id = insert_product(sku, "Ordering target", "one row, many committed versions", 100)

    def first_doc() -> bool:
        document = _clean_product_document(product_id)
        return bool(document and document.get("_deleted") is False)

    sysio.wait_for(first_doc, 30, 0.3, "ordering target initial document")
    sysio.wait_for(
        lambda: sysio.monitor("/staleness").get("sample_count", 0)
        >= samples_before + 1,
        30,
        0.5,
        "ordering target first sample",
    )

    burst_sql = "".join(
        f"UPDATE products SET price_cents = {1000 + index}, "
        f"in_stock = {str(bool(index % 2 == 0)).lower()} "
        f"WHERE id = {product_id};\n"
        for index in range(1, burst_size + 1)
    )
    sysio.psql(burst_sql)

    def drained() -> bool:
        metrics = sysio.monitor("/staleness")
        return (
            metrics.get("sample_count", 0) >= samples_before + 1 + burst_size
            and metrics.get("in_flight_count", 0) == 0
            and sysio.xpending_count() == 0
        )

    sysio.wait_for(drained, 180, 1.0, "ordering burst fully processed")

    entries = _stream_events_after(stream_before["last_id"])
    events = sorted(
        _events_for_product(_decode_entries(entries), product_id),
        key=lambda event: (lsn_value(event["commit_lsn"]), event["stream_id"]),
    )
    if len(events) != burst_size + 1:
        raise DemoError(
            f"expected {burst_size + 1} events for product {product_id}, "
            f"found {len(events)}"
        )
    newest = events[-1]
    final_lsn = newest["commit_lsn"]
    document = _clean_product_document(product_id)
    if document is None:
        raise DemoError(f"final product document missing for {product_id}")
    clean, bad_keys = ev.clean_key_check(document)

    markers = _markers_for_events(events)
    markers_by_event = {marker["event_id"]: marker for marker in markers}
    missing = [event["event_id"] for event in events if event["event_id"] not in markers_by_event]
    superseded = [
        {"event_id": event["event_id"], "commit_lsn": event["commit_lsn"]}
        for event in events
        if markers_by_event.get(event["event_id"], {}).get("result") == "superseded"
    ]
    applied = [
        {"event_id": event["event_id"], "commit_lsn": event["commit_lsn"]}
        for event in events
        if markers_by_event.get(event["event_id"], {}).get("result") == "applied"
    ]
    consistent = (
        not missing
        and len(applied) + len(superseded) == len(events)
        and all(
            lsn_value(item["commit_lsn"]) < lsn_value(final_lsn)
            for item in superseded
        )
        and all(
            markers_by_event[event["event_id"]]["commit_lsn"] == event["commit_lsn"]
            for event in events
        )
    )
    final_matches_newest = (
        document.get("_lsn") == final_lsn
        and document.get("price_cents") == 1000 + burst_size
        and document.get("in_stock") is (burst_size % 2 == 0)
    )
    passed = final_matches_newest and consistent and clean
    metrics_after = sysio.monitor("/staleness")
    result: dict[str, Any] = {
        "name": "B",
        "title": "Ordering (same-row burst)",
        "status": "PASS" if passed else "FAIL",
        "product_id": product_id,
        "sku": sku,
        "committed_versions": len(events),
        "burst_updates": burst_size,
        "event_ids": [event["event_id"] for event in events],
        "commit_lsns": [event["commit_lsn"] for event in events],
        "newest_commit_lsn": final_lsn,
        "final_document_lsn": document.get("_lsn"),
        "final_matches_newest": final_matches_newest,
        "final_document": document,
        "document_clean": clean,
        "leaked_sdk_keys": bad_keys,
        "marker_count": len(markers),
        "marker_count_matches_events": not missing,
        "missing_marker_event_ids": missing,
        "superseded_count": len(superseded),
        "superseded_events": superseded,
        "applied_count": len(applied),
        "applied_events": applied,
        "markers_consistent": consistent,
        "monitor_samples_before": samples_before,
        "monitor_samples_after": metrics_after.get("sample_count"),
        "violation_count_delta": metrics_after.get("violation_count", 0)
        - metrics_before.get("violation_count", 0),
        "elapsed_seconds": round((now_us() - started) / 1_000_000, 1),
    }
    return result


def _run_loadgen(rate: float, duration: float, log_path: Path) -> tuple[int, int]:
    loadgen_env = os.environ.copy()
    loadgen_env["LOADGEN_RATE"] = str(rate)
    loadgen_env["LOADGEN_DURATION_SECONDS"] = str(duration)
    with log_path.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            ["docker", "compose", "--profile", "workload", "run", "--rm", "loadgen"],
            cwd=str(cfg.REPO),
            env=loadgen_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        returncode = process.wait(timeout=max(60, int(duration * 4)))
    lines = log_path.read_text(encoding="utf-8").splitlines()
    write_records = [
        json.loads(line)
        for line in lines
        if line.strip().startswith("{") and '"event":"loadgen_write"' in line
    ]
    return len(write_records), returncode


def slo_workload(run_dir: Path, rate: float = 5.0, duration: float = 12.0) -> dict[str, Any]:
    """Produce a real mixed workload whose samples back the SLO check."""
    samples_before = sysio.monitor("/staleness").get("sample_count", 0)
    writes, returncode = _run_loadgen(
        rate, duration, run_dir / "logs" / "slo_loadgen.log"
    )

    def drained() -> bool:
        metrics = sysio.monitor("/staleness")
        return (
            metrics.get("sample_count", 0) >= samples_before + writes
            and metrics.get("in_flight_count", 0) == 0
            and sysio.xpending_count() == 0
        )

    sysio.wait_for(drained, 180, 1.0, "SLO workload samples recorded")
    metrics = sysio.monitor("/staleness")
    return {
        "writes": writes,
        "returncode": returncode,
        "samples_before": samples_before,
        "samples_after": metrics.get("sample_count"),
        "violation_count_after": metrics.get("violation_count"),
    }


def scenario_c(run_dir: Path, outage_seconds: float = 14.0) -> dict[str, Any]:
    started = now_us()
    monitor_before = sysio.monitor("/staleness")
    stream_before = sysio.stream_state()
    dlq_before = sysio.dlq_length()
    pending_before = sysio.xpending_count()
    lag_before = sysio.slot_lag_bytes()
    samples_before = monitor_before.get("sample_count", 0)
    violations_before = monitor_before.get("violation_count", 0)

    loadgen_log = run_dir / "logs" / "scenario_c_loadgen.log"
    loadgen_env = os.environ.copy()
    loadgen_env["LOADGEN_RATE"] = "3"
    loadgen_env["LOADGEN_DURATION_SECONDS"] = "40"
    with loadgen_log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            ["docker", "compose", "--profile", "workload", "run", "--rm", "loadgen"],
            cwd=str(cfg.REPO),
            env=loadgen_env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )

        time.sleep(6)
        growth_before_stop = sysio.stream_state()["length"] - stream_before["length"]
        sysio.compose("stop", "indexer")
        sysio.wait_for(
            lambda: sysio.container_status("indexer").startswith("exited"),
            40,
            1.0,
            "indexer stopped",
        )

        outage_samples: list[dict[str, Any]] = []
        stop_clock = time.monotonic()
        while time.monotonic() - stop_clock < outage_seconds:
            metrics = sysio.monitor("/staleness")
            outage_samples.append(
                {
                    "seconds_since_stop": round(time.monotonic() - stop_clock, 1),
                    "stream_length": sysio.stream_state()["length"],
                    "violation_count": metrics.get("violation_count"),
                    "active_violation_count": metrics.get("active_violation_count"),
                    "oldest_in_flight_age_ms": metrics.get("oldest_in_flight_age_ms"),
                    "pending": sysio.xpending_count(),
                }
            )
            time.sleep(2)

        sysio.compose("start", "indexer")
        sysio.wait_for(
            lambda: sysio.service_ready_via_exec("indexer", 8081),
            120,
            2.0,
            "indexer healthy after restart",
        )

        def recovered() -> bool:
            if process.poll() is None:
                return False
            metrics = sysio.monitor("/staleness")
            return (
                metrics.get("in_flight_count", 0) == 0
                and metrics.get("active_violation_count", 0) == 0
                and sysio.xpending_count() == 0
            )

        sysio.wait_for(recovered, 240, 2.0, "indexer backlog drain and recovery")
        returncode = process.wait(timeout=30)

    loadgen_lines = loadgen_log.read_text(encoding="utf-8").splitlines()
    write_records = [
        json.loads(line)
        for line in loadgen_lines
        if line.strip().startswith("{") and '"event":"loadgen_write"' in line
    ]
    writes = len(write_records)

    monitor_after = sysio.monitor("/staleness")
    stream_after = sysio.stream_state()
    dlq_after = sysio.dlq_length()
    pending_after = sysio.xpending_count()
    lag_after = sysio.slot_lag_bytes()
    samples_after = monitor_after.get("sample_count", 0)
    violations_after = monitor_after.get("violation_count", 0)

    sample_delta = samples_after - samples_before
    stream_delta = stream_after["length"] - stream_before["length"]
    dlq_delta = dlq_after - dlq_before
    violation_delta = violations_after - violations_before
    counts_consistent = sample_delta == writes and stream_delta == writes
    lag_recovered = all(
        lag_after.get(slot, 0) <= lag_before.get(slot, 0) + 5_000_000
        for slot in ("cdc_products_slot", "staleness_monitor_slot")
    )
    verdict = bool(
        counts_consistent
        and dlq_delta == 0
        and pending_after == 0
        and monitor_after.get("active_violation_count", 0) == 0
        and lag_recovered
        and returncode == 0
    )

    result: dict[str, Any] = {
        "name": "C",
        "title": "Failure and recovery (indexer stopped mid-workload)",
        "status": "PASS" if verdict else "FAIL",
        "loadgen": {
            "rate_per_second": 3,
            "duration_seconds": 40,
            "writes": writes,
            "returncode": returncode,
        },
        "indexer_stop": {
            "stopped_after_seconds_of_workload": 6,
            "backlog_entries_before_stop": growth_before_stop,
            "outage_seconds": round(outage_seconds, 1),
            "backlog_growth_samples": outage_samples,
        },
        "recovery": {
            "pending_before": pending_before,
            "pending_after": pending_after,
            "dlq_before": dlq_before,
            "dlq_after": dlq_after,
            "dlq_delta": dlq_delta,
            "samples_before": samples_before,
            "samples_after": samples_after,
            "sample_delta": sample_delta,
            "stream_length_before": stream_before["length"],
            "stream_length_after": stream_after["length"],
            "stream_delta": stream_delta,
            "writes": writes,
            "counts_consistent": counts_consistent,
            "slot_lag_bytes_before": lag_before,
            "slot_lag_bytes_after": lag_after,
            "wal_lag_recovered": lag_recovered,
            "active_violations_after": monitor_after.get("active_violation_count"),
        },
        "violations": {
            "count_before": violations_before,
            "count_after": violations_after,
            "violations_during_run": violation_delta,
            "active_after": monitor_after.get("active_violation_count", 0),
            "p99_detection_delay_ms": monitor_after.get("p99_detection_delay_ms"),
            "max_staleness_ms": monitor_after.get("max_staleness_ms"),
        },
        "monitor_after": monitor_after,
        "elapsed_seconds": round((now_us() - started) / 1_000_000, 1),
    }
    return result


def run_slo_verifier(minimum_samples: int) -> dict[str, Any]:
    url = f"{cfg.Config().monitor_url}/staleness"
    result = subprocess.run(
        [
            sys.executable,
            "tests/slo/verify_slo.py",
            "--url",
            url,
            "--minimum-samples",
            str(minimum_samples),
        ],
        cwd=str(cfg.REPO),
        capture_output=True,
        text=True,
        timeout=180,
    )
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        pass
    return {
        "exit_status": result.returncode,
        "measurements": parsed,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def diagram(scenario: dict[str, Any], lines: list[str]) -> str:
    title = f"{scenario['name']} — {scenario['title']} [{scenario.get('status')}]"
    return title + "\n" + "\n".join(lines)
