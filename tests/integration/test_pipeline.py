from __future__ import annotations

import json
import os
import time
import unittest
import urllib.request

import psycopg2


@unittest.skipUnless(os.environ.get("RUN_INTEGRATION") == "1", "set RUN_INTEGRATION=1")
class PipelineIntegrationTest(unittest.TestCase):
    def test_insert_update_delete_are_measured(self):
        monitor_url = os.environ.get("MONITOR_URL", "http://localhost:8080/staleness")
        dsn = os.environ["DATABASE_URL"]
        before = self.metrics(monitor_url)["sample_count"]
        sku = f"integration-{time.time_ns()}"
        with psycopg2.connect(dsn) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO products (sku, name, description, category, price_cents, in_stock)
                VALUES (%s, 'integration', 'integration', 'test', 100, true)
                RETURNING id
                """,
                (sku,),
            )
            product_id = cursor.fetchone()[0]
            connection.commit()
            cursor.execute(
                "UPDATE products SET price_cents = 200, updated_at = clock_timestamp() WHERE id = %s",
                (product_id,),
            )
            connection.commit()
            cursor.execute("DELETE FROM products WHERE id = %s", (product_id,))
            connection.commit()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            metrics = self.metrics(monitor_url)
            if metrics["sample_count"] >= before + 3:
                self.assertEqual(metrics["active_violation_count"], 0)
                return
            time.sleep(0.2)
        self.fail("monitor did not record insert, update, and delete within 20 seconds")

    @staticmethod
    def metrics(url):
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.load(response)


if __name__ == "__main__":
    unittest.main()
