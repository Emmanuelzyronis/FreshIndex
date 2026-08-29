"""Generate a controlled mix of real product inserts, updates, and deletes."""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import time
from typing import Any

import psycopg2


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate PostgreSQL product writes")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--rate", type=float, default=float(os.environ.get("LOADGEN_RATE", "5")))
    parser.add_argument(
        "--duration",
        type=float,
        default=float(os.environ.get("LOADGEN_DURATION_SECONDS", "60")),
        help="seconds to run; zero runs until stopped",
    )
    parser.add_argument("--seed", type=int, default=int(os.environ.get("LOADGEN_SEED", "2026")))
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or DATABASE_URL is required")
    if args.rate <= 0:
        parser.error("--rate must be greater than zero")
    return args


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, **fields}, separators=(",", ":")), flush=True)


def run(args: argparse.Namespace) -> int:
    stopped = False

    def stop(*_: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    randomizer = random.Random(args.seed)
    known_ids: list[int] = []
    sequence = 0
    started = time.monotonic()
    next_write = started

    with psycopg2.connect(args.dsn, application_name="cdc-load-generator") as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            while not stopped and (args.duration == 0 or time.monotonic() - started < args.duration):
                now = time.monotonic()
                if now < next_write:
                    time.sleep(min(next_write - now, 0.1))
                    continue
                sequence += 1
                choice = randomizer.random()
                if not known_ids or choice < 0.60:
                    sku = f"load-{args.seed}-{sequence}-{time.time_ns()}"
                    cursor.execute(
                        """
                        INSERT INTO products
                            (sku, name, description, category, price_cents, in_stock)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            sku,
                            f"Load product {sequence}",
                            "Generated CDC verification workload",
                            randomizer.choice(("books", "electronics", "home", "sports")),
                            randomizer.randint(100, 100_000),
                            True,
                        ),
                    )
                    product_id = int(cursor.fetchone()[0])
                    known_ids.append(product_id)
                    operation = "insert"
                elif choice < 0.90:
                    product_id = randomizer.choice(known_ids)
                    cursor.execute(
                        """
                        UPDATE products
                        SET price_cents = %s, in_stock = %s, updated_at = clock_timestamp()
                        WHERE id = %s
                        """,
                        (
                            randomizer.randint(100, 100_000),
                            randomizer.choice((True, False)),
                            product_id,
                        ),
                    )
                    operation = "update"
                else:
                    product_id = randomizer.choice(known_ids)
                    cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
                    known_ids.remove(product_id)
                    operation = "delete"
                log("loadgen_write", sequence=sequence, operation=operation, product_id=product_id)
                next_write += 1.0 / args.rate

    log("loadgen_finished", writes=sequence, duration_seconds=round(time.monotonic() - started, 3))
    return 0


def main() -> int:
    return run(arguments())


if __name__ == "__main__":
    raise SystemExit(main())
