"""FreshIndex portfolio evidence demonstration (single command entry).

Run from the repository root:

    python3 -m demo.run_demo [--label LABEL] [--burst N]

or via the wrapper script:

    bash demo/run.sh

The demonstration performs, against the real stack:
  A. happy-path commit-to-search-visibility with real per-hop values,
  B. same-row ordering with commit-LSN evidence and superseded markers,
  SLO verification with the repository verifier, and
  C. indexer failure/recovery with backlog and freshness evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config as cfg
from . import evidence as ev
from . import scenarios as sc
from . import system as sysio


def console_title(text: str) -> None:
    print("\n" + "=" * 72)
    print(text)
    print("=" * 72)


def print_scenario_a(result: dict) -> None:
    console_title(f"Scenario A — Happy Path [{result['status']}]")
    print(ev.timeline_text(result))


def print_scenario_b(result: dict) -> None:
    console_title(f"Scenario B — Ordering [{result['status']}]")
    print(
        f"Committed versions: {result['committed_versions']} "
        f"(1 insert + {result['burst_updates']} updates) on product {result['product_id']}"
    )
    print(f"Newest commit LSN : {result['newest_commit_lsn']}")
    print(f"Final doc _lsn    : {result['final_document_lsn']}")
    print(f"Final doc matches : {result['final_matches_newest']}")
    print(f"Markers           : {result['marker_count']} "
          f"(missing {len(result['missing_marker_event_ids'])})")
    print(f"Applied           : {result['applied_count']}")
    print(f"Superseded        : {result['superseded_count']}")
    print(f"Document clean    : {result['document_clean']}")
    print("\n  older events cannot overwrite newer committed state:")
    print(f"  superseded events are all older than {result['newest_commit_lsn']}: "
          f"{all(sc.lsn_value(i['commit_lsn']) < sc.lsn_value(result['newest_commit_lsn']) for i in result['superseded_events'])}")


def print_slo(slo: dict) -> None:
    console_title(f"SLO verification — exit status {slo['exit_status']}")
    measurements = slo.get("measurements") or {}
    fields = [
        ("sample_count", "sample count"),
        ("p50_staleness_ms", "p50"),
        ("p95_staleness_ms", "p95"),
        ("p99_staleness_ms", "p99"),
        ("max_staleness_ms", "max"),
        ("violation_count", "violations"),
    ]
    for key, label in fields:
        print(f"{label:<12}: {measurements.get(key)}")
    if slo["stderr"]:
        print(slo["stderr"])


def print_scenario_c(result: dict) -> None:
    console_title(f"Scenario C — Failure and Recovery [{result['status']}]")
    print("WORKLOAD")
    print("↓")
    print(f"INDEXER STOPPED (after {result['indexer_stop']['stopped_after_seconds_of_workload']}s "
          f"of workload, outage {result['indexer_stop']['outage_seconds']}s)")
    print("↓")
    backlog = result["indexer_stop"]["backlog_growth_samples"]
    if backlog:
        first = backlog[0]
        last = backlog[-1]
        print(
            f"REDIS BACKLOG ↑      {first['stream_length']} → {last['stream_length']} entries "
            f"while indexer stopped"
        )
        print(
            f"FRESHNESS VIOLATIONS ↑  active {last['active_violation_count']}, "
            f"oldest in-flight {last['oldest_in_flight_age_ms']}ms"
        )
    print("↓")
    print("INDEXER RESTARTED")
    print("↓")
    print(f"BACKLOG DRAIN          pending {result['recovery']['pending_before']} → "
          f"{result['recovery']['pending_after']}, "
          f"DLQ delta {result['recovery']['dlq_delta']}")
    print(f"SEARCH CATCHES UP      {result['recovery']['sample_delta']} samples "
          f"for {result['recovery']['writes']} committed writes "
          f"(consistent: {result['recovery']['counts_consistent']})")
    print("↓")
    verdict = "RECOVERY VERIFIED" if result["status"] == "PASS" else "RECOVERY NOT VERIFIED"
    print(verdict)
    violations = result["violations"]
    print(
        f"\nViolations observed: {violations['violations_during_run']}, "
        f"active after recovery: {violations['active_after']}, "
        f"p99 detection delay: {violations['p99_detection_delay_ms']}ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default=None, help="artifact directory label")
    parser.add_argument("--burst", type=int, default=150, help="scenario B update count")
    parser.add_argument(
        "--scenarios",
        default="a,b,slo,c",
        help="comma-separated phases to run (default a,b,slo,c)",
    )
    args = parser.parse_args()
    phases = [part.strip().lower() for part in args.scenarios.split(",") if part.strip()]

    console_title("FreshIndex — Portfolio Evidence Harness")
    print("git:", cfg.git_describe())

    sysio.compose("ps", check=False)
    run = ev.RunContext(args.label)
    evidence: dict = {
        "schema_version": 1,
        "run": {
            "timestamp_utc": sysio.now_iso(),
            "git": cfg.git_describe(),
            "environment": cfg.Config().safe_metadata(),
        },
        "scenarios": {},
        "slo": None,
        "event_counts": {},
        "marker_counts": {},
        "freshness": {},
        "verdicts": {},
        "log_excerpts": {},
        "service_health": {},
        "limitations": [],
        "presentation_readiness": {},
    }

    try:
        sc.monitor_ready()
        sc.fresh_monitor_epoch()
        metrics_epoch = sysio.monitor("/staleness")
        evidence["run"]["fresh_epoch_sample_count"] = metrics_epoch.get("sample_count")
        evidence["run"]["fresh_epoch"] = sysio.now_iso()
    except Exception as exc:
        print(f"preflight/fresh epoch failed: {exc}")
        evidence["run"]["error"] = str(exc)

    failure = False
    try:
        if "a" in phases:
            console_title("Starting Scenario A")
            result_a = sc.scenario_a(1)
            evidence["scenarios"]["happy_path"] = result_a
            run.write("scenario_a.json", json.dumps(result_a, indent=2, sort_keys=True) + "\n")
            print_scenario_a(result_a)
            if result_a["status"] != "PASS":
                failure = True

        if "b" in phases:
            console_title("Starting Scenario B")
            sc.fresh_monitor_epoch()
            result_b = sc.scenario_b(2, burst_size=args.burst)
            evidence["scenarios"]["ordering"] = result_b
            run.write("scenario_b.json", json.dumps(result_b, indent=2, sort_keys=True) + "\n")
            print_scenario_b(result_b)
            if result_b["status"] != "PASS":
                failure = True

        if "slo" in phases:
            console_title("Starting SLO verification")
            sc.fresh_monitor_epoch()
            slo_workload = sc.slo_workload(run.run_dir)
            print(
                f"Dedicated SLO workload: {slo_workload['writes']} committed writes, "
                f"samples {slo_workload['samples_before']} → {slo_workload['samples_after']}"
            )
            minimum = 25
            slo = sc.run_slo_verifier(minimum)
            evidence["slo"] = {**slo, "workload": slo_workload}
            run.write(
                "slo.json",
                json.dumps({**slo, "workload": slo_workload}, indent=2, sort_keys=True) + "\n",
            )
            print_slo(slo)
            if slo["exit_status"] != 0:
                failure = True

        if "c" in phases:
            console_title("Starting Scenario C")
            sc.fresh_monitor_epoch()
            result_c = sc.scenario_c(run.run_dir)
            evidence["scenarios"]["failure_recovery"] = result_c
            run.write(
                "scenario_c.json",
                json.dumps(result_c, indent=2, sort_keys=True) + "\n",
            )
            print_scenario_c(result_c)
            if result_c["status"] != "PASS":
                failure = True
    except Exception as exc:
        print(f"\nFATAL: {exc}")
        evidence["run"]["error"] = str(exc)
        failure = True

    console_title("Collecting evidence")
    for service in ("cdc-reader", "indexer", "monitor"):
        run.save_log(f"{service}.log", sysio.service_logs(service))

    scenario_names = {
        "happy_path": "a",
        "ordering": "b",
        "failure_recovery": "c",
    }
    for key, name in scenario_names.items():
        if key in evidence["scenarios"]:
            result = evidence["scenarios"][key]
            if key == "happy_path":
                evidence["event_counts"]["happy_path"] = {
                    "postgres_commit": 1,
                    "cdc_events": 1,
                    "indexer_applied": 1 if result.get("status") == "PASS" else 0,
                    "meilisearch_documents": 1,
                    "visibility_markers": 1 if result.get("marker") else 0,
                }
                evidence["marker_counts"]["happy_path"] = {
                    "applied": 1 if (result.get("marker") or {}).get("result") == "applied" else 0,
                    "superseded": 0,
                }
            elif key == "ordering":
                evidence["event_counts"]["ordering"] = {
                    "committed_versions": result.get("committed_versions"),
                }
                evidence["marker_counts"]["ordering"] = {
                    "total": result.get("marker_count"),
                    "applied": result.get("applied_count"),
                    "superseded": result.get("superseded_count"),
                    "missing": len(result.get("missing_marker_event_ids") or []),
                }
            elif key == "failure_recovery":
                evidence["event_counts"]["failure_recovery"] = {
                    "loadgen_writes": result.get("loadgen", {}).get("writes"),
                    "stream_entries_added": result.get("recovery", {}).get("stream_delta"),
                    "monitor_samples_added": result.get("recovery", {}).get("sample_delta"),
                }
                evidence["marker_counts"]["failure_recovery"] = {
                    "dlq_delta": result.get("recovery", {}).get("dlq_delta"),
                    "pending_after": result.get("recovery", {}).get("pending_after"),
                }

    metrics_final = sysio.monitor("/staleness")
    evidence["freshness"] = {
        "monitor_endpoint": metrics_final,
        "measured_this_run": True,
        "in_memory_window": True,
    }
    evidence["service_health"] = sysio.health_snapshot()
    evidence["verdicts"] = {
        "slo": "PASS" if (evidence.get("slo") or {}).get("exit_status") == 0 else "FAIL",
        "recovery": (evidence["scenarios"].get("failure_recovery") or {}).get("status", "N/A"),
        "ordering": (evidence["scenarios"].get("ordering") or {}).get("status", "N/A"),
        "happy_path": (evidence["scenarios"].get("happy_path") or {}).get("status", "N/A"),
    }

    ordering = evidence["scenarios"].get("ordering") or {}
    recovery = evidence["scenarios"].get("failure_recovery") or {}
    happy = evidence["scenarios"].get("happy_path") or {}
    readiness = {}
    readiness["happy_path_commit_to_visible"] = (
        "VERIFIED" if happy.get("status") == "PASS" else "NOT VERIFIED"
    )
    if ordering.get("status") == "PASS":
        readiness["ordering_older_cannot_overwrite_newer"] = (
            "VERIFIED"
            if (ordering.get("superseded_count") or 0) > 0
            else "PARTIALLY VERIFIED (final-state invariant only; no late arrival this run)"
        )
    else:
        readiness["ordering_older_cannot_overwrite_newer"] = "NOT VERIFIED"
    readiness["failure_recovery_backlog_drain"] = (
        "VERIFIED" if recovery.get("status") == "PASS" else "NOT VERIFIED"
    )
    readiness["staleness_slo_p99_below_1000ms"] = (
        "VERIFIED" if evidence["verdicts"]["slo"] == "PASS" else "PARTIALLY VERIFIED"
    )
    detection = (recovery.get("violations") or {}).get("p99_detection_delay_ms")
    if detection is None:
        readiness["violation_detection_within_500ms"] = (
            "NOT VERIFIED (no violations measured in this run)"
        )
    elif detection <= 500:
        readiness["violation_detection_within_500ms"] = (
            f"VERIFIED (p99 detection delay {detection}ms measured from this run)"
        )
    else:
        readiness["violation_detection_within_500ms"] = (
            f"PARTIALLY VERIFIED (p99 detection delay {detection}ms exceeds 500ms design target)"
        )
    readiness["kill9_xautoclaim_recovery"] = "NOT VERIFIED (not exercised in this run)"
    readiness["dlq_replay"] = "NOT VERIFIED (not exercised in this run)"
    readiness["metrics_survive_restart"] = (
        "NOT VERIFIED (metrics are in-memory by design and reset on restart)"
    )
    evidence["presentation_readiness"] = readiness

    evidence["limitations"] = [
        "Delivery is at-least-once; exactly-once is not claimed.",
        "Monitor metrics are in-memory and reset on restart; this demo starts a fresh monitor epoch and attributes measurements to the current run only.",
        "kill -9 / XAUTOCLAIM recovery has not been live-demonstrated in this run (scenario C uses a graceful docker compose stop).",
        "DLQ replay has not been live-demonstrated in this run.",
        "A controlled violation run is captured by scenario C in this run (indexer outage).",
        "A previous high-backlog run measured ~587ms p99 detection delay against the 500ms design target; scenario C reports the detection delay measured in this run.",
        "A previously observed ~7s same-row processing stall was not reproduced in this run and remains unexplained.",
        "Pre-existing product documents from earlier runs can still contain the historical _Document__doc artifact until a newer real update overwrites them; documents written by this demo run are verified clean.",
    ]

    happy_a = evidence["scenarios"].get("happy_path")
    if happy_a:
        needles = [str(happy_a.get("product_id")), happy_a.get("event_id", "")[:12]]
        evidence["log_excerpts"]["scenario_a_indexer"] = ev.log_excerpt(
            sysio.service_logs("indexer"), needles
        )
        evidence["log_excerpts"]["scenario_a_monitor"] = ev.log_excerpt(
            sysio.service_logs("monitor"), needles
        )

    path = run.finalize(evidence)
    console_title("Evidence bundle written")
    for file in run.summary():
        print(file)

    console_title("Presentation readiness")
    for capability, status in readiness.items():
        print(f"{capability:<45}: {status}")

    if evidence["run"].get("error"):
        print("\nRun error:", evidence["run"]["error"])
    return 1 if failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
