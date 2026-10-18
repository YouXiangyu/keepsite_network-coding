"""Windows worker lifetime binding without additional dependencies.

An unnamed, non-inheritable Job Object is owned only by the supervisor. Windows
closes that handle even on supervisor termination, killing its assigned worker.
API layouts/flags: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', ctypes.c_uint32),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', ctypes.c_uint32),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', ctypes.c_uint32),
        ('SchedulingClass', ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount',
    )]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class WindowsProcessJob:
    """Own the sole Job Object handle, with KILL_ON_JOB_CLOSE enabled."""

    def __init__(self):
        self._handle = None
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        self._kernel = kernel
        # NULL security attributes mean the returned handle cannot be inherited.
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if self._handle is None:
            raise RuntimeError('cannot assign a worker to a closed job')
        # CPython's Windows Popen retains the CreateProcess handle. Use that
        # exact process object, not a PID lookup that could target a reused PID.
        if not self._kernel.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle is not None:
            if not self._kernel.CloseHandle(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = None


class _NonWindowsJob:
    """Other platforms retain the supervisor's existing explicit termination."""

    def assign(self, process):
        pass

    def close(self):
        pass


def new_process_job():
    return WindowsProcessJob() if os.name == 'nt' else _NonWindowsJob()


def start_managed_process(command, **options):
    """Create the guard first and fail closed if process assignment fails.

    Return (Popen, guard); the caller must close the guard in its stage finally.
    Only the supervisor owns the guard, so abnormal exit closes its last handle.
    """
    guard = new_process_job()
    process = None
    try:
        process = subprocess.Popen(command, **options)
        guard.assign(process)
        return process, guard
    except BaseException:
        try:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        finally:
            guard.close()
        raise
