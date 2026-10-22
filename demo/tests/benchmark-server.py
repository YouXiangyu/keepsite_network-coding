"""M6 isolated dataset profile; serves only the separately built benchmark client."""
from browser_fixture import benchmark_profile, run_fixture


if __name__ == "__main__":
    run_fixture(benchmark_profile())
