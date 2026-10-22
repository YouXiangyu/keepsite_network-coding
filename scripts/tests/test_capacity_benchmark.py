"""Small synthetic baseline checks; no timing thresholds or external services."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("capacity_benchmark", ROOT / "scripts/benchmark-capacity.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class CapacityBenchmarkTests(unittest.TestCase):
    def setUp(self):
        # Tests own only a new child in this D: checkout, never the system temp
        # directory or a preexisting benchmark/production workspace.
        parent = ROOT / ".cache"
        parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="capacity-test-owned-", dir=parent)
        self.root = Path(self.temporary.name).resolve()
        self.assertEqual(self.root.parent, parent.resolve())
        self.addCleanup(self.temporary.cleanup)

    def test_profiles_counts_sql_and_read_only_measurement(self):
        sentinel = self.root / "keep.txt"
        sentinel.write_text("do not remove", encoding="utf-8")
        # Poison ambient defaults: explicit injection must ignore these locations.
        with patch.dict(os.environ, {"MEMOVERSE_DATA_DIR": str(self.root / "must-not-open-data"),
                                     "MEMOVERSE_STORAGE": "s3",
                                     "MEMOVERSE_S3_BUCKET": "must-not-access-cloud",
                                     "MEMOVERSE_REDIS_URL": "redis://must-not-connect.invalid:6379/0"}):
            report = benchmark.run_benchmark(self.root, counts=(4, 8), warmup=1, samples=2, sql_samples=2)
        self.assertEqual(len(report["cases"]), 4)
        self.assertEqual(report["application_schema_version"], 3)
        self.assertTrue(report["method"]["sql_work_in_timing"])
        self.assertFalse(report["method"]["sql_trace_in_timing"])
        self.assertNotIn("sql_in_timing", report["method"])
        self.assertTrue(report["method"]["temporary_data_cleaned"])
        for case in report["cases"]:
            count = case["tickets"]
            self.assertEqual(case["successful_timed_snapshots"], 2)
            self.assertEqual(case["http_requests_per_snapshot"], 1)
            self.assertEqual(case["timed_http_requests"], 2)
            self.assertGreater(case["response_body_bytes"], 0)
            self.assertEqual(case["response_content_encoding"], "identity")
            self.assertEqual(case["row_counts"]["tickets"], count)
            self.assertEqual(case["row_counts"]["users"], 1)
            self.assertEqual(set(case["row_counts"]), {"users", "tickets", "assets", "asset_grants", "operations", "ticket_changes", "sync_baselines", "upload_intents"})
            self.assertEqual(case["row_counts"]["operations"], 0)
            self.assertEqual(case["row_counts"]["ticket_changes"], 0)
            self.assertEqual(case["row_counts"]["upload_intents"], 0)
            self.assertGreater(case["sql"]["select_min"], 0)
            self.assertEqual(case["sql"]["select_min"], case["sql"]["select_max"])
            self.assertEqual(case["sql"]["samples"], 2)
            for trace in case["sql"]["statements_by_kind"]:
                self.assertLessEqual(set(trace), {"SELECT", "BEGIN", "COMMIT"})
            self.assertEqual(case["read_checks"], {"snapshot_and_revision_unchanged": True,
                                                  "row_counts_unchanged": True, "traced_write_statements": 0})
            self.assertEqual(case["behavior_checks"], {"create_replay": True, "field_update": True,
                                                      "delete_tombstone": True, "account_isolation": True,
                                                      "media_roundtrip": True, "deleted_media_denied": True})
            self.assertLessEqual(case["latency_ms"]["p50"], case["latency_ms"]["p95"])
            self.assertLessEqual(case["latency_ms"]["p95"], case["latency_ms"]["max"])
            images = case["media_manifest"]
            self.assertEqual(images["image_references"], count)
            self.assertGreater(images["unique_synthetic_image_bytes"], 0)
            if case["profile"] == "shared-original":
                self.assertEqual(case["row_counts"]["assets"], 1)
                self.assertEqual(images["unique_media_urls"], 1)
            else:
                self.assertEqual(case["row_counts"]["assets"], count)
                self.assertEqual(images["unique_media_urls"], count)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not remove")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"keep.txt"})

    def test_existing_database_root_is_rejected_without_reading_or_changing_it(self):
        database = self.root / "memoverse.sqlite3"
        database.write_bytes(b"not a database: an untouched sentinel")
        with patch.object(benchmark, "create_app", side_effect=AssertionError("must not open")):
            with self.assertRaisesRegex(ValueError, "existing account workspace"):
                benchmark.run_benchmark(self.root, counts=(1,), warmup=0, samples=1, sql_samples=1)
        self.assertEqual(database.read_bytes(), b"not a database: an untouched sentinel")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"memoverse.sqlite3"})

    def test_invalid_arguments_do_not_create_a_scratch_directory(self):
        destination = self.root / "not-created"
        for options in ({"counts": (0,)}, {"counts": (10000,)}, {"counts": (10001,)}, {"counts": (1, 1)}, {"samples": 0}, {"sql_samples": 0},
                        {"warmup": -1}, {"profiles": ("unknown",)}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                benchmark.run_benchmark(destination, **options)
        self.assertFalse(destination.exists())
        with self.assertRaisesRegex(ValueError, "absolute"):
            benchmark.run_benchmark(Path("relative"))

    def test_failure_cleans_only_the_new_owned_directory(self):
        sentinel = self.root / "keep.txt"
        sentinel.write_text("untouched", encoding="utf-8")

        def fail(case_root, *args):
            case_root.mkdir(parents=True)
            (case_root / "synthetic.tmp").write_text("owned", encoding="utf-8")
            raise RuntimeError("injected failure")

        with patch.object(benchmark, "_sample_case", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                benchmark.run_benchmark(self.root, counts=(1,), warmup=0, samples=1, sql_samples=1)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"keep.txt"})
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched")

    def test_reports_are_new_json_files_and_never_replace_previous_evidence(self):
        output = self.root / "report.json"
        report = {"cases": [], "note": "合成基线"}
        benchmark.write_report(output, report)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
        with self.assertRaises(FileExistsError):
            benchmark.write_report(output, {"different": True})
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
        with self.assertRaisesRegex(ValueError, "json"):
            benchmark.write_report(self.root / "not-a-report.sqlite3", report)


if __name__ == "__main__":
    unittest.main()
