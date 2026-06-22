from __future__ import annotations

import os

import httpx
import pytest

os.environ.setdefault("ENVIRONMENT", "test")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end tests that require a live running service (skipped by default; use -m e2e)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list) -> None:
    """Auto-skip e2e tests when no live service is reachable.

    This allows ``pytest tests/`` to run cleanly in CI without a running
    service. To run e2e tests explicitly, use ``make test-e2e`` (which starts
    the service first) or ``pytest -m e2e`` against a running instance.

    Auto-skip is suppressed when ``-m e2e`` is explicitly passed so that
    a deliberate e2e run against a missing service fails loudly instead of
    silently skipping.
    """
    marker_expr = getattr(config.option, "markexpr", "")
    if "e2e" in str(marker_expr):
        return  # user explicitly selected e2e — don't interfere

    base = os.environ.get("BASE_URL", "http://127.0.0.1:8090").rstrip("/")
    try:
        httpx.get(f"{base}/health", timeout=2.0)
    except Exception:
        skip = pytest.mark.skip(
            reason=f"e2e: service not reachable at {base} — run 'make test-e2e'"
        )
        for item in items:
            if item.get_closest_marker("e2e"):
                item.add_marker(skip)
