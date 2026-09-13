#!/usr/bin/env node
/**
 * Normalizes a FreshIndex evidence run into video/src/data/artifact.json.
 *
 * Reads the newest demo/artifacts/<timestamp>/ bundle and emits the single
 * shape the film's data layer consumes. Re-run this after any fresh evidence
 * run to rebuild the film against new real numbers.
 *
 *   node scripts/sync-evidence.mjs [artifact-dir]
 *
 * When the live source is wired later, fromLive.ts produces this same shape —
 * scenes never change.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../..');
const artifactsDir = path.join(repo, 'demo', 'artifacts');

function newestArtifact() {
  const dirs = fs
    .readdirSync(artifactsDir)
    .filter((d) => fs.existsSync(path.join(artifactsDir, d, 'evidence.json')))
    .sort();
  if (!dirs.length) throw new Error(`no evidence bundles in ${artifactsDir}`);
  return dirs[dirs.length - 1];
}

const explicit = process.argv[2];
const name = explicit ? path.basename(explicit) : newestArtifact();
const dir = path.join(artifactsDir, name);
const read = (f) => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));

const evidence = read('evidence.json');
const scenarioC = fs.existsSync(path.join(dir, 'scenario_c.json')) ? read('scenario_c.json') : {};

const a = evidence.scenarios.happy_path;
const b = evidence.scenarios.ordering;
const c = evidence.scenarios.failure_recovery ?? {};
const rec = c.recovery ?? {};
const after = c.monitor_after ?? {};
const loadgen = c.loadgen ?? {};

const growth = (scenarioC.indexer_stop?.backlog_growth_samples ?? []).map((s) => ({
  secondsSinceStop: s.seconds_since_stop,
  pending: s.pending,
  streamLength: s.stream_length,
  violationCount: s.violation_count,
  activeViolations: s.active_violation_count,
  oldestAgeMs: s.oldest_in_flight_age_ms,
}));

const out = {
  generatedFrom: name,
  generatedAt: new Date().toISOString(),
  meta: {
    artifact: name,
    runTimestampUtc: evidence.run?.timestamp_utc ?? null,
    gitCommit: evidence.run?.git?.commit_short ?? null,
    gitDirty: evidence.run?.git?.dirty ?? null,
    sloMs: Number(evidence.run?.environment?.config?.staleness_slo_ms ?? 1000),
    sloDetectionTargetMs: 500,
    indexerWorkers: Number(evidence.run?.environment?.config?.indexer_workers ?? 4),
    indexerBatchSize: Number(evidence.run?.environment?.config?.indexer_batch_size ?? 50),
    stack: evidence.run?.environment?.stack ?? 'docker compose',
  },
  verdicts: evidence.verdicts ?? {},

  happyPath: {
    productId: a.product_id,
    commitLsn: a.commit_lsn,
    eventId: a.event_id,
    commitToXaddMs: a.commit_to_xadd_ms,
    queueLatencyMs: a.indexer_stream_queue_latency_ms,
    batchProcessingMs: a.indexer_batch_processing_ms,
    stalenessMs: a.staleness_ms,
    markerResult: a.marker?.result ?? null,
    documentClean: a.document_clean,
    timeline: (a.timeline ?? []).map((t) => ({ stage: t.stage, value: t.value })),
  },

  ordering: {
    committedVersions: b.committed_versions,
    appliedCount: b.applied_count,
    supersededCount: b.superseded_count,
    newestCommitLsn: b.newest_commit_lsn,
    finalDocumentLsn: b.final_document_lsn,
    finalMatchesNewest: b.final_matches_newest,
    markerCount: b.marker_count,
    violationDelta: b.violation_count_delta ?? 0,
  },

  recovery: {
    writes: loadgen.writes ?? rec.writes ?? 0,
    ratePerSecond: loadgen.rate_per_second ?? 0,
    durationSeconds: loadgen.duration_seconds ?? 0,
    outageSeconds: scenarioC.indexer_stop?.outage_seconds ?? 0,
    peakViolationCount: after.violation_count ?? 0,
    peakActiveViolations: after.active_violation_count ?? 0,
    peakP99Ms: after.p99_staleness_ms ?? null,
    peakMaxMs: after.max_staleness_ms ?? null,
    pendingBefore: rec.pending_before ?? 0,
    pendingAfter: rec.pending_after ?? 0,
    dlqDelta: rec.dlq_delta ?? 0,
    dlqAfter: rec.dlq_after ?? 0,
    samples: rec.samples_after ?? after.sample_count ?? 0,
    walLagBefore: rec.slot_lag_bytes_before ?? {},
    walLagAfter: rec.slot_lag_bytes_after ?? {},
    walLagRecovered: rec.wal_lag_recovered ?? false,
    countsConsistent: rec.counts_consistent ?? false,
    growth,
  },

  slo: {
    sampleCount: evidence.slo?.measurements?.sample_count ?? 0,
    p50: evidence.slo?.measurements?.p50_staleness_ms ?? null,
    p95: evidence.slo?.measurements?.p95_staleness_ms ?? null,
    p99: evidence.slo?.measurements?.p99_staleness_ms ?? null,
    max: evidence.slo?.measurements?.max_staleness_ms ?? null,
    violations: evidence.slo?.measurements?.violation_count ?? 0,
    exitStatus: evidence.slo?.exit_status ?? null,
  },

  // From README.md "Validation status" (2026-09-01 run), kept separate so the
  // film can label its provenance distinctly from the current-run bundle.
  benchmark: {
    source: 'README.md — Validation status, 2026-09-01',
    samples: 300,
    throughputPerSecond: 5.0,
    p50: 151.994,
    p95: 207.06,
    p99: 221.839,
    max: 225.274,
    violations: 0,
  },
};

fs.mkdirSync(path.join(here, '../src/data'), { recursive: true });
fs.writeFileSync(path.join(here, '../src/data/artifact.json'), JSON.stringify(out, null, 2));
console.log(`synced ${name} -> src/data/artifact.json`);
