from __future__ import annotations

import argparse
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


load_generator_path = Path(__file__).parents[2] / "load-generator" / "main.py"
load_generator_spec = importlib.util.spec_from_file_location(
    "load_generator_main", load_generator_path
)
load_generator = importlib.util.module_from_spec(load_generator_spec)
assert load_generator_spec.loader is not None
with mock.patch.dict(sys.modules, {"psycopg2": types.SimpleNamespace(connect=None)}):
    load_generator_spec.loader.exec_module(load_generator)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.next_id = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, parameters):
        self.connection.commit_tokens.append(self.connection.next_commit_token)
        if self.connection.autocommit:
            self.connection.next_commit_token += 1

    def fetchone(self):
        product_id = self.next_id
        self.next_id += 1
        return (product_id,)


class FakeConnection:
    def __init__(self):
        self.autocommit = False
        self.closed = False
        self.next_commit_token = 1
        self.commit_tokens = []

    def __enter__(self):
        raise AssertionError("connection must not be used as a context manager")

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class LoadGeneratorConnectionTest(unittest.TestCase):
    def test_consecutive_mutations_are_independently_committed(self):
        connection = FakeConnection()
        args = argparse.Namespace(dsn="postgresql://test", rate=1.0, duration=2.0, seed=2026)

        with (
            mock.patch.object(load_generator.psycopg2, "connect", return_value=connection),
            mock.patch.object(load_generator.signal, "signal"),
            mock.patch.object(load_generator.time, "monotonic", side_effect=[0, 0, 0, 1, 1, 2, 2]),
            mock.patch.object(load_generator.time, "time_ns", side_effect=[1, 2]),
            mock.patch.object(load_generator, "log"),
        ):
            result = load_generator.run(args)

        self.assertEqual(result, 0)
        self.assertTrue(connection.autocommit)
        self.assertEqual(connection.commit_tokens, [1, 2])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
