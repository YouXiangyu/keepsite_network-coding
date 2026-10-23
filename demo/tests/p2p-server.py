"""M5 single-image profile, preserving its production client and loopback port."""
from browser_fixture import P2P_PROFILE, run_fixture


if __name__ == "__main__":
    run_fixture(P2P_PROFILE)
