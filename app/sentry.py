from __future__ import annotations

import sentry_sdk

from app.config import (
    ENVIRONMENT,
    SENTRY_DSN,
    SENTRY_PROFILE_SESSION_SAMPLE_RATE,
    SENTRY_TRACES_SAMPLE_RATE,
)


def init_sentry() -> bool:
    if not SENTRY_DSN:
        return False
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=ENVIRONMENT,
        send_default_pii=True,
        enable_logs=True,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profile_session_sample_rate=SENTRY_PROFILE_SESSION_SAMPLE_RATE,
        profile_lifecycle="trace",
    )
    return True
