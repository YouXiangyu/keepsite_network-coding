"""Protocol measurements must validate correctness and own all scratch data."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sync_benchmark", ROOT / "scripts/benchmark-sync.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class SyncBenchmarkTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".cache"
        parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="sync-benchmark-test-", dir=parent)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def test_incremental_workload_has_complete_state_and_exact_request_accounting(self):
        sentinel = self.root / "keep.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        with patch.dict(os.environ, {"KEEPSITE_DATA_DIR": str(self.root / "do-not-open"),
                                     "KEEPSITE_STORAGE": "s3", "KEEPSITE_S3_BUCKET": "do-not-access",
                                     "KEEPSITE_REDIS_URL": "redis://do-not-connect.invalid:6379/0"}):
            report = benchmark.run_case(ROOT, self.root, "incremental", 103)
        measured, checked = report["measurements"], report["checks"]
        self.assertEqual(measured["initialSync"]["requests"], 3)
        self.assertEqual(measured["steadySync"]["requests"], 3)
        self.assertEqual(measured["writerReceipts"]["requests"], 6)
        for measurement in measured.values():
            self.assertEqual(measurement["failedRequests"], 0)
            self.assertEqual(measurement["contentEncodings"], ["identity"])
            self.assertGreater(measurement["responseBodyBytes"], 0)
        self.assertEqual(checked["modelComparisons"], 13)
        self.assertEqual(checked["finalTickets"], 103)
        self.assertEqual(checked["observedCommitBarriers"], [2, 5, 6])
        self.assertTrue(checked["temporaryAccountRemoved"])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"keep.txt"})

    def test_invalid_arguments_and_existing_workspace_do_not_modify_data(self):
        for protocol, count in (("unknown", 3), ("incremental", 2), ("incremental", 10001)):
            with self.subTest(protocol=protocol, count=count), self.assertRaises(ValueError):
                benchmark.run_case(ROOT, self.root, protocol, count)
        with self.assertRaisesRegex(ValueError, "historical"):
            benchmark.run_case(ROOT, self.root, "full-poll", 3)
        sentinel = self.root / "keepsite.sqlite3"
        sentinel.write_bytes(b"untouched; not a database")
        with self.assertRaisesRegex(ValueError, "existing account"):
            benchmark.run_case(ROOT, self.root, "incremental", 3)
        self.assertEqual(sentinel.read_bytes(), b"untouched; not a database")

    def test_missing_history_is_rejected_without_advancing_cursor(self):
        class MissingChange:
            def request(self, *args, **kwargs):
                return {"userId": benchmark.USER_ID, "serverId": "server", "fromCursor": 10,
                        "nextCursor": 12, "headCursor": 12, "hasMore": False,
                        "changes": [{"cursor": 12, "type": "delete", "id": "ticket"}]}

        tickets = {"ticket": {"id": "ticket"}}
        with self.assertRaisesRegex(RuntimeError, "Non-contiguous"):
            benchmark.catch_up(MissingChange(), tickets, 10, "server", 12)
        self.assertIn("ticket", tickets)


if __name__ == "__main__":
    unittest.main()
