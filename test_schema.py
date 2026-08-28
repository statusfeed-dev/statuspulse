import sqlite3
import unittest
from contextlib import closing
from pathlib import Path


class SchemaTests(unittest.TestCase):
    def test_schema_creates_order_and_refund_state(self):
        schema = Path("schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(":memory:")) as database, database:
            database.executescript(schema)
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({"orders", "order_events", "refunds"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
