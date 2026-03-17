"""Tests for Sentry integration."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


class TestInitSentry:
    def test_returns_false_when_dsn_unset(self):
        with patch("app.sentry.SENTRY_DSN", None):
            from app.sentry import init_sentry
            assert init_sentry() is False

    def test_returns_true_and_calls_sdk_init(self):
        with patch("app.sentry.SENTRY_DSN", "https://fake@sentry.example.com/1"), \
             patch("app.sentry.sentry_sdk") as mock_sdk:
            from app.sentry import init_sentry
            assert init_sentry() is True
            mock_sdk.init.assert_called_once()


class TestSentryDebugRoute:
    def test_sentry_debug_exists_in_non_production(self):
        with patch.dict("os.environ", {"ENVIRONMENT": "development"}, clear=False):
            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/sentry-debug")
            # Route exists but raises ZeroDivisionError -> 500
            assert resp.status_code == 500
