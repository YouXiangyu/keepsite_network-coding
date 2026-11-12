import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("network_host_runner", ROOT / "scripts/run-network-host.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FakeAdb:
    serial = "emulator-5580"

    def __init__(self, overrides=False):
        self.identity = {"serial": self.serial, "avd": "OwnedTest", "fingerprint": "test/build",
                         "user": "0", "boot": "12345678-1234-1234-1234-123456789abc"}
        self.display = {
            "size": {"physical": "1080x1920", "override": "800x1200" if overrides else None},
            "density": {"physical": "420", "override": "280" if overrides else None},
            "rotation": "lock 2" if overrides else "free",
            "user_rotation": "2" if overrides else "null",
            "accelerometer_rotation": "0" if overrides else "1",
        }
        self.before = copy.deepcopy(self.display)
        self.calls = []
        self.test_output = "OK (3 tests)"
        self.test_exception = None
        self.fail_command = None
        self.running = False
        self.started = False

    def shell(self, *args):
        self.calls.append(args)
        if self.fail_command and args[:len(self.fail_command)] == self.fail_command:
            raise RuntimeError("injected command failure: " + " ".join(args))
        if args == ("getprop", "ro.kernel.qemu"):
            return "1"
        if args == ("getprop", "ro.build.fingerprint"):
            return self.identity["fingerprint"]
        if args == ("am", "get-current-user"):
            return self.identity["user"]
        if args == ("cat", "/proc/sys/kernel/random/boot_id"):
            return self.identity["boot"]
        if args == ("pm", "list", "instrumentation"):
            return f"instrumentation:{runner.COMPONENT} (target={runner.PACKAGE})"
        if args == ("ps", "-A", "-w", "-o", "NAME"):
            return "NAME\n" + (runner.PACKAGE if self.running else "system_server")
        if args[:2] == ("am", "force-stop"):
            self.running = False
            return ""
        if args[0] == "wm":
            key = args[1]
            if key == "user-rotation":
                if len(args) > 2:
                    self.display["rotation"] = " ".join(args[2:])
                    self.display["accelerometer_rotation"] = "1" if args[2] == "free" else "0"
                    self.display["user_rotation"] = args[3] if args[2] == "lock" else (
                        "0" if self.display["user_rotation"] == "null" else self.display["user_rotation"])
                return self.display["rotation"]
            if len(args) > 2:
                self.display[key]["override"] = None if args[2] == "reset" else args[2]
                return ""
            value = self.display[key]
            return f"Physical {key}: {value['physical']}" + (
                f"\nOverride {key}: {value['override']}" if value["override"] else "")
        if args[0] == "settings":
            operation, key = args[3], args[5]
            if operation == "get":
                return self.display[key]
            self.display[key] = args[6] if operation == "put" else "null"
            user_rotation = "0" if self.display["user_rotation"] == "null" else self.display["user_rotation"]
            self.display["rotation"] = "free" if self.display["accelerometer_rotation"] == "1" else "lock " + user_rotation
            return ""
        raise AssertionError(args)

    def call(self, *args, timeout=30, log=None):
        if args == ("emu", "avd", "name"):
            return self.identity["avd"] + "\nOK"
        if args[:3] == ("shell", "am", "instrument"):
            self.started = True
            self.running = True
            self.display.update(rotation="lock 1", user_rotation="1", accelerometer_rotation="0")
            self.display["size"]["override"] = "720x1600"
            self.display["density"]["override"] = "320"
            if log:
                log.write_text(self.test_output, encoding="utf-8")
            if self.test_exception:
                raise self.test_exception
            return self.test_output
        raise AssertionError(args)


class NetworkHostRunnerTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(runner.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        cache = ROOT / ".cache"
        cache.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "run"

    def record(self):
        return json.loads((self.output / "display-state.json").read_text(encoding="utf-8"))

    def test_success_restores_missing_overrides_and_nullable_setting(self):
        adb = FakeAdb()
        state = runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertTrue(state["restored"])
        self.assertEqual(state["tests"], "passed")
        self.assertIn(("wm", "size", "reset"), adb.calls)
        self.assertIn(("settings", "--user", "0", "delete", "system", "user_rotation"), adb.calls)

    def test_failed_test_restores_existing_overrides_and_locked_rotation(self):
        adb = FakeAdb(overrides=True)
        adb.test_output = "FAILURES!!!\nTests run: 3, Failures: 1"
        with self.assertRaisesRegex(RuntimeError, "all three original tests"):
            runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertTrue(self.record()["restored"])
        self.assertNotIn(("wm", "density", "reset"), adb.calls)

    def test_interrupt_restores_and_keeps_failure(self):
        adb = FakeAdb()
        adb.test_exception = KeyboardInterrupt()
        with self.assertRaises(RuntimeError):
            runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertEqual(self.record()["test_error"], "KeyboardInterrupt")
        self.assertFalse(self.record()["restored"])
        self.assertEqual(self.record()["command_completion"], "unknown")
        self.assertFalse(adb.running)

    def test_cleanup_failure_keeps_test_failure_and_attempts_other_fields(self):
        adb = FakeAdb()
        adb.test_exception = RuntimeError("original failure")
        adb.fail_command = ("wm", "size", "reset")
        with patch.object(runner.time, "sleep"), self.assertRaisesRegex(RuntimeError, "original failure.*restoration"):
            runner.run(adb, self.output)
        self.assertFalse(self.record()["restored"])
        self.assertEqual(adb.display["density"], adb.before["density"])
        self.assertEqual(adb.display["rotation"], adb.before["rotation"])
        self.assertIn("injected command failure", self.record()["restore_error"])

    def test_recover_pending_journal_without_rerunning_tests(self):
        adb = FakeAdb()
        adb.test_exception = RuntimeError("lost connection")
        adb.fail_command = ("wm", "density", "reset")
        with patch.object(runner.time, "sleep"), self.assertRaises(RuntimeError):
            runner.run(adb, self.output)
        adb.fail_command = None
        adb.started = False
        state = runner.run(adb, self.output, recover=True)
        self.assertFalse(adb.started)
        self.assertTrue(state["restored"])
        self.assertEqual(state["tests"], "failed_or_interrupted")
        self.assertEqual(adb.display, adb.before)
        with self.assertRaisesRegex(RuntimeError, "already restored"):
            runner.run(adb, self.output, recover=True)

    def test_recovery_refuses_different_boot_before_writing(self):
        adb = FakeAdb()
        self.output.mkdir()
        runner.save(self.output / "display-state.json", {"schema": 1, "identity": copy.deepcopy(adb.identity),
                    "before": adb.before, "restored": False, "tests": "running"})
        adb.identity["boot"] = "87654321-1234-1234-1234-123456789abc"
        with self.assertRaisesRegex(RuntimeError, "differs"):
            runner.run(adb, self.output, recover=True)
        self.assertFalse(any(call[:2] == ("am", "force-stop") for call in adb.calls))

    def test_unknown_snapshot_and_running_auxiliary_refuse_before_test(self):
        adb = FakeAdb()
        adb.display["rotation"] = "unknown"
        with self.assertRaisesRegex(ValueError, "rotation mode"):
            runner.run(adb, self.output)
        self.assertFalse(adb.started)
        self.assertFalse((self.output / "display-state.json").exists())
        adb = FakeAdb()
        adb.running = True
        with self.assertRaisesRegex(RuntimeError, "already running"):
            runner.run(adb, self.output.parent / "other")
        self.assertFalse(adb.started)

    def test_incomplete_or_failed_instrumentation_cannot_report_pass(self):
        for text in ("OK (2 tests)", "OK (3 tests)\nINSTRUMENTATION_FAILED: aborted", "INSTRUMENTATION_CODE: 0"):
            with self.subTest(text=text):
                adb = FakeAdb()
                adb.test_output = text
                directory = self.output.parent / str(len(list(self.output.parent.iterdir())))
                with self.assertRaises(RuntimeError):
                    runner.run(adb, directory)
                self.assertEqual(adb.display, adb.before)

    def test_no_physical_devices_or_malformed_recovery_values(self):
        with self.assertRaisesRegex(ValueError, "dedicated emulator"):
            runner.Adb("adb", 5081, "physical-serial")
        adb = FakeAdb()
        before = copy.deepcopy(adb.before)
        before["size"]["override"] = "720x1600; unexpected"
        with self.assertRaisesRegex(ValueError, "recovery value"):
            runner.restore(adb, adb.identity, before)
        self.assertFalse(any(call[0] == "wm" for call in adb.calls))

    def test_failed_final_journal_write_preserves_baseline_and_original_error(self):
        adb = FakeAdb()
        adb.test_exception = RuntimeError("original failure")
        original_save = runner.save
        writes = 0

        def fail_final(path, value):
            nonlocal writes
            writes += 1
            if writes == 3:
                raise OSError("disk write failure")
            original_save(path, value)

        with patch.object(runner, "save", side_effect=fail_final), self.assertRaisesRegex(
                RuntimeError, "original failure.*Journal update failed"):
            runner.run(adb, self.output)
        self.assertEqual(self.record()["before"], adb.before)
        self.assertFalse(self.record()["restored"])
        self.assertEqual(adb.display, adb.before)

    def test_stop_command_failure_still_restores_after_process_exit(self):
        adb = FakeAdb()
        adb.fail_command = ("am", "force-stop", runner.TEST_PACKAGE)
        with self.assertRaisesRegex(RuntimeError, "Cannot stop auxiliary"):
            runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertTrue(self.record()["restored"])

    def test_process_still_alive_prevents_racing_display_restoration(self):
        adb = FakeAdb()
        adb.fail_command = ("am", "force-stop")
        with patch.object(runner.time, "sleep"), self.assertRaisesRegex(RuntimeError, "still active"):
            runner.run(adb, self.output)
        self.assertFalse(self.record()["restored"])
        self.assertNotIn(("wm", "size", "reset"), adb.calls)

    def test_successful_test_with_restoration_failure_is_not_success(self):
        adb = FakeAdb()
        adb.fail_command = ("wm", "density", "reset")
        with patch.object(runner.time, "sleep"), self.assertRaisesRegex(RuntimeError, "restoration failed"):
            runner.run(adb, self.output)
        self.assertEqual(self.record()["tests"], "passed")
        self.assertFalse(self.record()["restored"])

    def test_initial_journal_failure_never_starts_instrumentation(self):
        adb = FakeAdb()
        with patch.object(runner, "save", side_effect=OSError("disk full")), self.assertRaisesRegex(OSError, "disk full"):
            runner.run(adb, self.output)
        self.assertFalse(adb.started)
        self.assertEqual(adb.display, adb.before)

    def test_absent_rotation_settings_restore_default_lock_zero(self):
        adb = FakeAdb()
        adb.display.update(rotation="lock 0", user_rotation="null", accelerometer_rotation="null")
        adb.before = copy.deepcopy(adb.display)
        runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertIn(("settings", "--user", "0", "delete", "system", "accelerometer_rotation"), adb.calls)

    def test_inconsistent_rotation_snapshot_refuses_mutations(self):
        adb = FakeAdb()
        adb.display["accelerometer_rotation"] = "null"
        with self.assertRaisesRegex(ValueError, "disagree"):
            runner.run(adb, self.output)
        self.assertFalse(adb.started)
        self.assertFalse((self.output / "display-state.json").exists())

    def test_uncertain_device_command_requires_explicit_recovery_after_readback(self):
        adb = FakeAdb()
        adb.test_output = "FAILURES!!!\nDISPLAY_COMMAND_UNCERTAIN: shell completion unknown"
        with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
            runner.run(adb, self.output)
        self.assertEqual(adb.display, adb.before)
        self.assertFalse(self.record()["restored"])
        state = runner.run(adb, self.output, recover=True)
        self.assertTrue(state["restored"])
        self.assertEqual(state["command_completion"], "explicit_recovery")

    def test_restore_waits_for_settings_observer_to_settle(self):
        adb = FakeAdb()
        original_snapshot = runner.snapshot
        reads = 0

        def delayed_snapshot(device, user):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise ValueError("Rotation mode and saved settings disagree")
            return original_snapshot(device, user)

        with patch.object(runner, "snapshot", side_effect=delayed_snapshot):
            self.assertEqual(runner.restore(adb, adb.identity, adb.before), adb.before)
        self.assertEqual(reads, 2)

    def test_emulator_console_blank_lines_keep_strict_identity(self):
        adb = FakeAdb()
        with patch.object(adb, "call", return_value="OwnedTest\n\nOK\n\n"):
            self.assertEqual(runner.identity(adb), adb.identity)
        with patch.object(adb, "call", return_value="OwnedTest\n\nunexpected\nOK\n"):
            with self.assertRaisesRegex(ValueError, "identify the emulator"):
                runner.identity(adb)


if __name__ == "__main__":
    unittest.main()
