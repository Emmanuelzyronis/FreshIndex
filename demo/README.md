# FreshIndex — Portfolio Evidence Harness

This directory turns the verified FreshIndex CDC pipeline into a repeatable,
professional demonstration. It is an **evidence/capture layer**, not a
product: no pipeline component is redesigned, no mock data is produced, and no
application UI is introduced. Every number in the output is read from the live
system through its real boundaries.

The portfolio story demonstrated with the real running system:

> PostgreSQL committed changes flow through logical CDC into Redis Streams,
> are applied to Meilisearch with ordering/idempotency guarantees, and are
> independently measured from database commit to confirmed search visibility.

## What the harness proves

- **Scenario A — happy path.** One deterministic product mutation is committed,
  then tracked through the PostgreSQL commit, the decoded CDC event, the Redis
  Stream delivery, indexer processing, the Meilisearch product document, the
  `cdc_visibility` marker, and the monitor's independent read-probe sample.
  A human-readable timeline is printed with real values.
- **Scenario B — ordering.** A burst of committed updates against one product
  proves the final Meilisearch document equals the newest committed version
  and that older late-arriving events are classified as `superseded` (an older
  event can never overwrite newer committed state).
- **SLO verification.** The repository verifier
  (`tests/slo/verify_slo.py`) runs against the real monitor endpoint after a
  dedicated mixed workload; sample count, p50/p95/p99, max, violations, and
  verifier exit status are recorded from the current run.
- **Scenario C — failure and recovery.** The indexer is stopped (only the
  indexer) while real writes continue. Redis backlog growth and freshness
  violations are captured, the indexer is restarted, and the harness verifies
  backlog drain, pending → 0, active violations → 0, DLQ → 0, consistent
  event/marker counts, and recovered WAL lag.

## Requirements

- A host running the FreshIndex stack: `docker compose up -d --build`
  from the repository root, with `.env` configured.
- `docker compose` and `python3` (standard library only for the harness).
- Optional evidence-page capture: `google-chrome` (or any Chromium binary), or
  Playwright pointed at the proxy URL.

## Run the complete demonstration

```bash
cd /path/to/FreshIndex
docker compose up -d --build        # fresh stack; see note below for pristine runs
bash demo/run.sh
```

This runs Scenario A, Scenario B, SLO verification, and Scenario C, then
writes the evidence bundle to:

```text
demo/artifacts/<timestamp>/evidence.json
demo/artifacts/<timestamp>/scenario_a.json
demo/artifacts/<timestamp>/scenario_b.json
demo/artifacts/<timestamp>/slo.json
demo/artifacts/<timestamp>/scenario_c.json
demo/artifacts/<timestamp>/logs/{cdc-reader,indexer,monitor}.log
```

Partial runs are supported, e.g. `bash demo/run.sh --scenarios a,slo`.
Use `--label my-run` to name the artifact directory.

The exit code is non-zero if any selected scenario fails its checks.

### Pristine, comparable runs

The monitor's metrics are in-memory and reset on restart. The demo restarts
the monitor before each measurement phase so every window is attributable to
that phase. For a fully clean database/index, run
`docker compose down -v` before the standard setup; without that, documents,
stream entries, and markers from earlier runs remain (the demo's own rows and
counts are always isolated by product id / time window).

## How the measurements are made

| Value | Source |
| --- | --- |
| PostgreSQL commit timestamp/LSN | pgoutput commit record (reader event) plus a direct `pg_xact_commit_timestamp` query for the same transaction |
| Redis Stream delivery | real `XADD` (`published_ts_us`) and message id from the stream |
| Indexer processing | real `indexer_event` log records (`stream_queue_latency_ms`, `batch_processing_ms`) |
| Meilisearch application | real product document `_lsn` and `cdc_visibility` marker via the Meilisearch HTTP API |
| Visibility / staleness | monitor `staleness_sample` log (independent second replication slot + read-probe) |
| Backlog / pending / DLQ | real `XLEN`, `XPENDING`, `XLEN cdc_events_dlq` |
| WAL lag | `pg_replication_slots` confirmed-flush lag for both slots |
| SLO | `python tests/slo/verify_slo.py` against the real `/staleness` endpoint |

## Example scenario output

Scenario A prints the real per-hop timeline:

```text
POSTGRES COMMIT  commit_ts 2026-09-09T19:12:58.608560+00:00 lsn 0/1A92B60
↓
CDC EVENT  event_id ea076a53b11a… xid 2551 pg_commit_ts_confirmed 2026-09-09 19:12:58.60856+00
↓
REDIS STREAM  stream 1782 → 1783 message 1788981178612-0 commit→xadd 3.664ms
↓
INDEXER  result=applied queue_latency 1.349ms batch_processing 127.649ms
↓
MEILISEARCH  product id=619 _lsn=0/1A92B60
↓
VISIBLE  marker ea076a53b11a… result=applied
↓
STALENESS  150.278ms (monitor read-probe, SLO 1000.0ms)
```

## Reading the ordering result

Scenario B commits 150 rapid updates to one row. Processing is serialized per
product key, so the final document must always equal the newest committed LSN;
events that are overtaken by newer committed state are recorded as
`superseded` markers. A representative result shows 151 committed versions,
one marker per version, a handful of `superseded` markers (late-arriving older
events), and a final document whose `_lsn` equals the newest commit LSN.

Note that a same-key burst is intentionally serialized; tail events can age
past the SLO while waiting for the key. That is ordering behavior, not a
freshness regression, which is why SLO verification runs on a separate
mixed-key workload in its own monitor window.

## Reading the failure/recovery result

Scenario C stops only the indexer for ~14 seconds while real writes continue.
The stream backlog grows, monitor freshness violations rise, and after the
indexer restart the harness checks: pending → 0, active violations → 0,
DLQ delta → 0, stream events added == loadgen writes == monitor samples, and
both replication slots recovered. The printed verdict is based on those real
numbers.

## Evidence bundle

`evidence.json` contains run metadata (timestamp, git commit, safe
environment/config), scenario results, event and marker counts, freshness
measurements, SLO/recovery/ordering verdicts, relevant log excerpts, and the
final service health state. Secrets are never written: the writer refuses to
emit a bundle containing any configured secret value and redacts them if an
error message accidentally includes one (e.g. a command line containing an
environment variable).

## Optional evidence page capture

`demo/evidence_page/index.html` is a tiny static page that displays the
pipeline diagram and reads **real** endpoints only — the monitor
`/health`, `/ready`, `/staleness`, `/metrics`, Redis pending/DLQ, indexer
queue state, and a real Meilisearch search — through
`demo/evidence_page/server.py`, a read-only local proxy bound to 127.0.0.1.
No mock values appear.

## Portfolio capture

After a real run has written `evidence.json`, capture the portfolio bundle with
the existing Node Playwright runtime:

```bash
node demo/capture_portfolio.mjs demo/artifacts/<timestamp>/portfolio 8778
```

This writes `hero.png`, `architecture.png`, `freshindex-demo.webm`, and a copy
of the run's `evidence.json`. If `ffmpeg` is available, the capture workflow
also writes `freshindex-demo.mp4`; WebM remains the required portable output.
The page server is read-only and binds to `127.0.0.1`.

```bash
python3 -m demo.evidence_page.server --port 8777   # open http://127.0.0.1:8777/
python3 -m demo.capture_evidence_page --out demo/artifacts/evidence_page.png
```

The capture script uses headless Chrome; pointing Playwright at the same proxy
URL yields the same evidence page (screenshot/video/trace) without any change
to FreshIndex.

## Honest limitations preserved by this harness

- Delivery is at-least-once; exactly-once is not claimed.
- Monitor metrics are in-memory and reset on restart; measurements are
  attributed to the run's own monitor epoch.
- `kill -9` / `XAUTOCLAIM` recovery is not live-demonstrated by Scenario C,
  which uses a graceful `docker compose stop`.
- DLQ replay is not live-demonstrated.
- A previous high-backlog run measured ~587 ms p99 detection delay against the
  ~500 ms design target; the current run reports the detection delay it
  actually measured.
- A previously observed ~7 s same-row processing stall was not reproduced and
  remains unexplained. Same-row burst queueing (Scenario B) is measured and
  shown to be serialized per key by design.
- Documents written before the data-hygiene fix may still contain the
  historical `_Document__doc` artifact until a newer real update overwrites
  them; documents written by the demo run are verified clean.

The `evidence.json` `presentation_readiness` section states, per capability,
`VERIFIED`, `PARTIALLY VERIFIED`, or `NOT VERIFIED` based on what this run
actually measured — it never silently promotes partial verification.
