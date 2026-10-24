"""Reject obsolete configuration before opening persistent application data."""
import os


def require_current_environment():
    if any(name.startswith("MEMOVERSE_") for name in os.environ):
        raise RuntimeError(
            "Legacy MEMOVERSE_* environment detected: migrate configuration to KEEPSITE_* "
            "and remove the old variables before starting keepsite. No configuration values were printed."
        )
