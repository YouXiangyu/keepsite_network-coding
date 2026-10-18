"""Real short-lived subprocess checks for lifetime binding; never run models."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from experiments.subject_relief import lab_process


ROOT = Path(__file__).resolve().parents[2]
HIDDEN = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
SLEEPER = [sys.executable, '-B', '-c', 'import time; time.sleep(30)']


def stop_owned(process):
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object integration')
def test_job_structure_uses_pointer_sized_members():
    assert ctypes.sizeof(lab_process._ExtendedLimits) == (144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object integration')
def test_closing_last_job_handle_terminates_assigned_worker():
    process, guard = lab_process.start_managed_process(
        SLEEPER, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN,
    )
    try:
        assert process.poll() is None
        guard.close()
        process.wait(timeout=5)
        assert process.poll() is not None  # Job-close termination need not use a nonzero exit code.
        guard.close()  # Stage cleanup must be safe after an earlier close.
    finally:
        guard.close()
        stop_owned(process)


@pytest.mark.skipif(os.name != 'nt', reason='Windows Job Object integration')
def test_force_terminating_supervisor_also_terminates_worker(tmp_path):
    ready = tmp_path / 'owned-child.pid'
    code = r'''
import subprocess, sys, time
from pathlib import Path
from experiments.subject_relief.lab_process import start_managed_process
child, guard = start_managed_process(
    [sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NO_WINDOW,
)
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.partial')
temporary.write_text(str(child.pid), encoding='ascii')
temporary.replace(ready)
time.sleep(30)
'''
    parent = subprocess.Popen([sys.executable, '-B', '-c', code, str(ready)], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    child_handle = None
    try:
        deadline = time.monotonic() + 8
        while not ready.exists() and parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), f'test supervisor failed before assigning its child: {parent.poll()}'
        child_pid = int(ready.read_text(encoding='ascii'))
        # Keep a handle to this precise child for both waiting and cleanup;
        # never kill by a PID that could have been recycled later.
        child_handle = kernel.OpenProcess(0x00100000 | 0x0001, False, child_pid)
        assert child_handle, ctypes.WinError(ctypes.get_last_error())
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # WAIT_TIMEOUT: alive
        parent.kill()  # No Python finally, atexit or service shutdown runs.
        parent.wait(timeout=5)
        assert kernel.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        stop_owned(parent)
        if child_handle:
            if kernel.WaitForSingleObject(child_handle, 0) != 0:
                kernel.TerminateProcess(child_handle, 1)
                kernel.WaitForSingleObject(child_handle, 5000)
            kernel.CloseHandle(child_handle)


def test_assignment_failure_reaps_child_and_closes_guard(monkeypatch):
    class FailingGuard:
        process = None
        closed = False

        def assign(self, process):
            self.process = process
            raise OSError('controlled AssignProcessToJobObject failure')

        def close(self):
            self.closed = True

    guard = FailingGuard()
    monkeypatch.setattr(lab_process, 'new_process_job', lambda: guard)
    try:
        with pytest.raises(OSError, match='AssignProcess'):
            lab_process.start_managed_process(SLEEPER, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=HIDDEN)
        assert guard.closed
        assert guard.process.poll() is not None
        assert guard.process.stdout.closed and guard.process.stderr.closed
    finally:
        if guard.process is not None:
            stop_owned(guard.process)


def test_popen_failure_closes_precreated_guard(monkeypatch):
    class Guard:
        closed = False

        def assign(self, process):
            pytest.fail('no process should have been created')

        def close(self):
            self.closed = True

    guard = Guard()
    monkeypatch.setattr(lab_process, 'new_process_job', lambda: guard)
    with pytest.raises(OSError):
        lab_process.start_managed_process(['nonexistent-relief-test-executable-179362.exe'], creationflags=HIDDEN)
    assert guard.closed


def test_job_creation_failure_never_launches_a_process(monkeypatch):
    def fail():
        raise OSError('controlled CreateJobObject failure')

    monkeypatch.setattr(lab_process, 'new_process_job', fail)
    monkeypatch.setattr(lab_process.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must fail before spawn'))
    with pytest.raises(OSError, match='CreateJobObject'):
        lab_process.start_managed_process(SLEEPER)
