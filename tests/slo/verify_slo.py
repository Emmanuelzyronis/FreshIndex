from __future__ import annotations

import argparse
import json
import time
import urllib.request


def metrics(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify measured CDC staleness")
    parser.add_argument("--url", default="http://localhost:8080/staleness")
    parser.add_argument("--minimum-samples", type=int, default=100)
    parser.add_argument("--staleness-bound-ms", type=float, default=1000)
    parser.add_argument("--detection-bound-ms", type=float, default=500)
    parser.add_argument("--violation-test", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    observed = {}
    while time.monotonic() < deadline:
        observed = metrics(args.url)
        if observed.get("sample_count", 0) >= args.minimum_samples:
            break
        time.sleep(1)
    else:
        raise SystemExit(
            f"only {observed.get('sample_count', 0)} samples; expected {args.minimum_samples}"
        )

    p99 = observed.get("p99_staleness_ms")
    if not args.violation_test and (p99 is None or p99 > args.staleness_bound_ms):
        raise SystemExit(f"p99 staleness {p99} ms exceeds {args.staleness_bound_ms} ms")
    if args.violation_test:
        if observed.get("violation_count", 0) < 1:
            raise SystemExit("no SLO violation was observed")
        detection = observed.get("p99_detection_delay_ms")
        if detection is None or detection > args.detection_bound_ms:
            raise SystemExit(
                f"p99 detection delay {detection} ms exceeds {args.detection_bound_ms} ms"
            )
    print(json.dumps(observed, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
