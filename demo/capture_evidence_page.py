"""Capture a screenshot of the live evidence page with headless Chrome.

Evidence-capture only: it starts the read-only local proxy, opens the static
page that reads the real /health, /ready, /staleness, /metrics and
Meilisearch endpoints, and saves a screenshot. No application UI is added to
FreshIndex itself. The same page can be captured with Playwright instead of
headless Chrome by pointing the browser at the proxy URL.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import time
from pathlib import Path

from .evidence_page import server as page_server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="PNG output path")
    parser.add_argument(
        "--chrome",
        default="google-chrome",
        help="chrome/chromium binary (default google-chrome)",
    )
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    out = Path(args.out) if args.out else Path("demo/artifacts/evidence_page.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    port = args.port or free_port()
    server = page_server.ThreadingHTTPServer(("127.0.0.1", port), page_server.Handler)
    url = f"http://127.0.0.1:{port}/"

    try:
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        time.sleep(1)
        result = subprocess.run(
            [
                args.chrome,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=1440,1200",
                "--virtual-time-budget=6000",
                f"--screenshot={out}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            print(result.stderr[:500])
            return result.returncode
        print(out)
        return 0
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
