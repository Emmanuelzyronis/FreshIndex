"""Capture the live FreshIndex evidence page into a portfolio artifact bundle."""
from __future__ import annotations

import argparse
import shutil
import threading
import time
from pathlib import Path

from .evidence_page import server as page_server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="portfolio output directory")
    parser.add_argument("--port", type=int, default=8778)
    parser.add_argument("--seconds", type=int, default=75)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server = page_server.ThreadingHTTPServer(("127.0.0.1", args.port), page_server.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}/"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        server.shutdown()
        raise SystemExit("Playwright is required for video capture: pip install playwright") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, record_video_dir=str(out))
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(4500)
        page.screenshot(path=str(out / "hero.png"), full_page=False)
        page.locator(".architecture").screenshot(path=str(out / "architecture.png"))
        # The real page state is captured while moving through each evidence section.
        for selector in [".hero", ".section:nth-of-type(1)", ".section:nth-of-type(2)", ".section:nth-of-type(3)", ".section:nth-of-type(4)"]:
            page.locator(selector).scroll_into_view_if_needed()
            page.wait_for_timeout(max(3500, args.seconds * 1000 // 5))
        page.locator(".hero").scroll_into_view_if_needed()
        page.wait_for_timeout(2500)
        page.close()
        video_path = Path(page.video.path())
        target = out / "freshindex-demo.webm"
        shutil.move(video_path, target)
        browser.close()
    server.shutdown()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        import subprocess
        subprocess.run([ffmpeg, "-y", "-i", str(target), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out / "freshindex-demo.mp4")], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    evidence = sorted((Path(__file__).resolve().parent / "artifacts").glob("*/evidence.json"))[-1]
    shutil.copy2(evidence, out / "evidence.json")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
