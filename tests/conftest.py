import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end tests that require a live running service (skipped by default; use -m e2e)",
    )
