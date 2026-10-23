"""Atomic directory publication for isolated local data copies."""
import ctypes
import errno
import os
from pathlib import Path
import sys


def publish_directory_noreplace(source: Path, destination: Path):
    """Atomically expose a prepared tree without replacing any destination entry."""
    if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()) for parent in destination.parents):
        raise OSError(errno.ELOOP, "Destination ancestors must not contain links", str(destination))
    if os.name == "nt":
        # Windows rename fails if any destination entry already exists.
        os.rename(source, destination)
        return
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "Atomic non-replacing directory publication is unavailable", str(destination))
    # POSIX rename alone can replace another caller's empty directory. Do not
    # fall back to a check-then-rename race on unsupported kernels/filesystems.
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOTSUP, "renameat2 is required for directory publication", str(destination))
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:  # AT_FDCWD, RENAME_NOREPLACE
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
