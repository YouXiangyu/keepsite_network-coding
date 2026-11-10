"""Run the isolated network UI tests and restore the emulator display settings."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = "io.github.youxiangyu.keepsite.networkverification"
TEST_PACKAGE = PACKAGE + ".test"
COMPONENT = TEST_PACKAGE + "/androidx.test.runner.AndroidJUnitRunner"
TEST_CLASS = "io.github.youxiangyu.keepsite.networkfixture.NetworkHostInstrumentedTest"


class Adb:
    def __init__(self, executable, port, serial):
        if not re.fullmatch(r"emulator-\d+", serial):
            raise ValueError("Use a dedicated emulator, not a physical device")
        self.prefix = [executable, "-P", str(port), "-s", serial]
        self.serial = serial

    def call(self, *args, timeout=30, log=None):
        if log is not None:
            with log.open("w", encoding="utf-8") as stream:
                result = subprocess.run(self.prefix + list(args), stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=timeout)
            if result.returncode:
                raise RuntimeError(f"Instrumentation adb exited {result.returncode}; see {log}")
            return log.read_text(encoding="utf-8", errors="replace").strip()
        result = subprocess.run(self.prefix + list(args), capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"adb {' '.join(args[:3])}: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout.strip()

    def shell(self, *args):
        return self.call("shell", "toybox", "timeout", "-s", "KILL", "5", *args)


def dimension(text, label, pattern):
    lines = text.splitlines()
    physical = re.fullmatch(rf"Physical {label}: ({pattern})", lines[0]) if lines else None
    override = re.fullmatch(rf"Override {label}: ({pattern})", lines[1]) if len(lines) == 2 else None
    if not physical or len(lines) > 2 or (len(lines) == 2 and not override):
        raise ValueError(f"Unrecognized wm {label} output: {text!r}")
    return {"physical": physical[1], "override": override[1] if override else None}


def setting(adb, user, key, pattern):
    value = adb.shell("settings", "--user", user, "get", "system", key)
    if not re.fullmatch(pattern, value):
        raise ValueError(f"Unknown {key}: {value!r}")
    return value


def identity(adb):
    if adb.shell("getprop", "ro.kernel.qemu") != "1":
        raise ValueError("Only a dedicated Android emulator is supported")
    avd = [line.strip() for line in adb.call("emu", "avd", "name").splitlines() if line.strip()]
    if not avd or len(avd) > 2 or (len(avd) == 2 and avd[1] != "OK") or not re.fullmatch(r"[\w.-]+", avd[0]):
        raise ValueError("Cannot identify the emulator AVD")
    fingerprint = adb.shell("getprop", "ro.build.fingerprint")
    user = adb.shell("am", "get-current-user")
    boot = adb.shell("cat", "/proc/sys/kernel/random/boot_id")
    if not fingerprint or not re.fullmatch(r"\d+", user) or not re.fullmatch(r"[0-9a-f-]{36}", boot):
        raise ValueError("Cannot identify build/current Android user")
    return {"serial": adb.serial, "avd": avd[0], "fingerprint": fingerprint, "user": user, "boot": boot}


def snapshot(adb, user):
    rotation = adb.shell("wm", "user-rotation")
    if not re.fullmatch(r"free|lock [0-3]", rotation):
        raise ValueError(f"Unrecognized rotation mode: {rotation!r}")
    state = {
        "size": dimension(adb.shell("wm", "size"), "size", r"[1-9]\d*x[1-9]\d*"),
        "density": dimension(adb.shell("wm", "density"), "density", r"[1-9]\d*"),
        "rotation": rotation,
        "user_rotation": setting(adb, user, "user_rotation", r"null|[0-3]"),
        "accelerometer_rotation": setting(adb, user, "accelerometer_rotation", r"null|[01]"),
    }
    expected = "free" if state["accelerometer_rotation"] == "1" else "lock " + (
        "0" if state["user_rotation"] == "null" else state["user_rotation"])
    if state["rotation"] != expected:
        raise ValueError("Rotation mode and saved settings disagree; refusing an unstable baseline")
    if adb.shell("am", "get-current-user") != user:
        raise ValueError("Android user changed while capturing display settings")
    return state


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def restore(adb, saved_identity, before):
    if identity(adb) != saved_identity:
        raise RuntimeError("Emulator identity/user changed; refusing to restore another target")
    user = saved_identity["user"]
    for key, pattern in (("size", r"[1-9]\d*x[1-9]\d*"), ("density", r"[1-9]\d*")):
        for field in ("physical", "override"):
            value = before[key][field]
            if not (field == "override" and value is None) and not re.fullmatch(pattern, str(value)):
                raise ValueError("Invalid display recovery value")
    for key, pattern in (("rotation", r"free|lock [0-3]"), ("user_rotation", r"null|[0-3]"),
                         ("accelerometer_rotation", r"null|[01]")):
        if not re.fullmatch(pattern, before[key]):
            raise ValueError("Invalid rotation recovery value")
    errors = []
    commands = [("wm", key, before[key]["override"] or "reset") for key in ("size", "density")]
    commands.append(("wm", "user-rotation", *before["rotation"].split()))
    for key in ("user_rotation", "accelerometer_rotation"):
        value = before[key]
        commands.append(("settings", "--user", user, "delete", "system", key) if value == "null"
                        else ("settings", "--user", user, "put", "system", key, value))
    for command in commands:
        try:
            adb.shell(*command)
        except Exception as failure:
            errors.append(str(failure))
    # Settings observers may apply asynchronously.
    after = None
    last_read_error = None
    for _ in range(20):
        try:
            after = snapshot(adb, user)
            last_read_error = None
            if after == before:
                break
        except ValueError as failure:
            last_read_error = str(failure)
        except Exception as failure:
            errors.append(str(failure))
            break
        time.sleep(0.1)
    if after != before:
        errors.append(f"Display read-back mismatch: {last_read_error or after!r}")
    if errors:
        raise RuntimeError("Display restoration failed: " + "; ".join(errors))
    return after


def stop_auxiliary(adb):
    errors = []
    for package in (TEST_PACKAGE, PACKAGE):
        try:
            adb.shell("am", "force-stop", package)
        except Exception as failure:
            errors.append(str(failure))
    if errors:
        raise RuntimeError("Cannot stop auxiliary instrumentation: " + "; ".join(errors))


def auxiliary_running(adb):
    lines = adb.shell("ps", "-A", "-w", "-o", "NAME").splitlines()
    if not lines or lines[0].strip() != "NAME" or any(
            len(line.split()) != 1 or "+" in line for line in lines[1:]):
        raise RuntimeError("Cannot verify auxiliary process state")
    return any(line.strip() == package or line.strip().startswith(package + ":")
               for line in lines[1:] for package in (PACKAGE, TEST_PACKAGE))


def await_auxiliary_stop(adb):
    for _ in range(50):
        if not auxiliary_running(adb):
            time.sleep(6)  # Allow already-started five-second shell commands to exit.
            return
        time.sleep(0.1)
    raise RuntimeError("Auxiliary process is still active; retain the journal and recover after it exits")


def run(adb, directory, recover=False):
    journal = directory / "display-state.json"
    if recover:
        state = json.loads(journal.read_text(encoding="utf-8"))
        if state.get("schema") != 1:
            raise ValueError("Unknown recovery record format")
        if identity(adb) != state["identity"]:
            raise RuntimeError("Recovery target differs from saved emulator/user")
        if state.get("restored"):
            raise RuntimeError("This run was already restored; do not reuse its baseline")
    else:
        directory.mkdir(parents=True, exist_ok=False)
        current = identity(adb)
        installed = adb.shell("pm", "list", "instrumentation")
        if f"instrumentation:{COMPONENT} (target={PACKAGE})" not in installed.splitlines():
            raise RuntimeError("Install the matching auxiliary application and test APKs first")
        if auxiliary_running(adb):
            raise RuntimeError("Auxiliary package is already running; stop it before starting a new run")
        state = {"schema": 1, "identity": current, "before": snapshot(adb, current["user"]),
                 "restored": False, "tests": "not_started"}
        save(journal, state)
    failure = None
    cleanup_failure = None
    completion_unknown = False
    try:
        if not recover:
            state["tests"] = "running"
            save(journal, state)
            completion_unknown = True
            output = adb.call("shell", "am", "instrument", "-w", "-r", "-e", "class", TEST_CLASS,
                              "-e", "displayGuard", "external-journal-v1", COMPONENT, timeout=600,
                              log=directory / "instrumentation.txt")
            completion_unknown = bool(re.search(
                r"DISPLAY_COMMAND_UNCERTAIN|Process crashed|INSTRUMENTATION_ABORTED", output)) or not (
                re.search(r"^OK \(3 tests\)$", output, re.MULTILINE) or "FAILURES!!!" in output)
            if not re.search(r"^OK \(3 tests\)$", output, re.MULTILINE) or re.search(
                    r"FAILURES!!!|INSTRUMENTATION_FAILED|Process crashed|INSTRUMENTATION_ABORTED|DISPLAY_COMMAND_UNCERTAIN", output):
                raise RuntimeError("Instrumentation did not complete all three original tests successfully")
            state["tests"] = "passed"
    except BaseException as error:
        failure = error
        state["tests"] = "failed_or_interrupted"
        state["test_error"] = str(error) or type(error).__name__
    finally:
        cleanup_errors = []
        try:
            if identity(adb) != state["identity"]:
                raise RuntimeError("Target changed; refusing cleanup on a different emulator/user")
            try:
                stop_auxiliary(adb)
            except Exception as error:
                cleanup_errors.append(str(error))
            await_auxiliary_stop(adb)
            state["after"] = restore(adb, state["identity"], state["before"])
            state["restored"] = not completion_unknown
            state["command_completion"] = "unknown" if completion_unknown else (
                "explicit_recovery" if recover else "completed")
            state.pop("restore_error", None)
            if completion_unknown:
                cleanup_errors.append("Command completion is unknown despite matching display read-back; "
                                      "confirm the emulator is idle, then use --recover")
        except BaseException as error:
            state["restored"] = False
            cleanup_errors.append(str(error) or type(error).__name__)
        if cleanup_errors:
            state["restore_error"] = "; ".join(cleanup_errors)
        try:
            save(journal, state)
        except Exception as error:
            cleanup_errors.append(f"Journal update failed; keep the previous baseline: {error}")
        if cleanup_errors:
            cleanup_failure = RuntimeError("; ".join(cleanup_errors))
    if failure or cleanup_failure:
        raise RuntimeError(f"Test: {failure or state['tests']}; restoration: {cleanup_failure or 'verified'}. "
                           f"Keep {journal}; use --recover if restoration is incomplete.") from failure
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--serial", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--output", type=Path, help="New directory for this run")
    modes.add_argument("--recover", type=Path, help="Directory with a previous display-state.json")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        result = run(Adb(args.adb, args.port, args.serial), args.output or args.recover, args.recover is not None)
        print(f"Tests: {result['tests']}; display restoration: verified")
        return 0
    except Exception as failure:
        print(str(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
