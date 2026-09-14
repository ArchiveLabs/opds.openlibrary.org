from __future__ import annotations


import os
from dotenv import load_dotenv

load_dotenv()

OPDS_MEDIA_TYPE = "application/opds+json"
OPDS_PUB_MEDIA_TYPE = "application/opds-publication+json"

ENVIRONMENT: str = os.environ.get("ENVIRONMENT", "production")
OPDS_BASE_URL: str | None = os.environ.get("OPDS_BASE_URL") or (
    "https://openlibrary.org/opds" if ENVIRONMENT == "production" else None
)
OL_BASE_URL: str = os.environ.get("OL_BASE_URL", "https://openlibrary.org")
OL_USER_AGENT: str = os.environ.get(
    "OL_USER_AGENT",
    "OPDSBot/1.0 (opds.openlibrary.org; opds@openlibrary.org)",
)
OL_REQUEST_TIMEOUT: float = float(os.environ.get("OL_REQUEST_TIMEOUT", "30.0"))

_nomad_addr = os.environ.get("NOMAD_ADDR_memcached", "")
_nomad_host, _, _nomad_port = _nomad_addr.partition(":")
MEMCACHE_HOST: str = os.environ.get("MEMCACHE_HOST") or _nomad_host or "localhost"
_memcache_port_raw = os.environ.get("MEMCACHE_PORT") or _nomad_port or "11211"
try:
    MEMCACHE_PORT: int = int(_memcache_port_raw)
except ValueError:
    raise ValueError(f"MEMCACHE_PORT must be an integer, got: {_memcache_port_raw!r}") from None
CACHE_ENABLED: bool = os.environ.get("CACHE_ENABLED", "true").lower() == "true"

# App-level CORS. Off by default: in production the fronting nginx already adds
# CORS headers, so enabling this here would emit a duplicate
# ``Access-Control-Allow-Origin`` and break browser clients. Enable it for local
# development (e.g. the Cloudflare-tunnel-to-reader.archive.org flow) where no
# nginx sits in front. See docs/testing-opds-locally.md.
CORS_ENABLED: bool = os.environ.get("CORS_ENABLED", "false").lower() == "true"

# --- iCloud asset relay -----------------------------------------------------
#
# A byte relay for one thing: the reader app putting a book into a reader's own
# iCloud from a *browser*. CloudKit's web-services asset upload POSTs to a
# signed URL on ``p<N>-contentws.icloud.com``, and that host answers no
# ``Access-Control-Allow-Origin`` — measured — so a browser can never read the
# upload receipt and the write cannot complete. Native shells have no such
# problem: their requests do not come from a browser at all.
#
# **Off unless ``ICLOUD_RELAY_ORIGINS`` is set**, and that is deliberate in a
# public repo: an unconfigured deployment 404s the route rather than offering
# anyone who runs this service an open byte relay. Comma-separated exact
# origins, e.g. "https://reader.archive.org,https://localhost:4173".
ICLOUD_RELAY_ORIGINS: list[str] = [
    origin.strip()
    for origin in os.environ.get("ICLOUD_RELAY_ORIGINS", "").split(",")
    if origin.strip()
]
ICLOUD_RELAY_ENABLED: bool = bool(ICLOUD_RELAY_ORIGINS)

# Apple's own ceiling for a web-services asset. The reader cuts files into 14 MB
# chunks, so a book of any size arrives as several requests, each under this.
ICLOUD_RELAY_MAX_BYTES: int = 15 * 1024 * 1024

# Generous: a 14 MB body on its way to Apple over somebody's home upload.
ICLOUD_RELAY_TIMEOUT: float = float(os.environ.get("ICLOUD_RELAY_TIMEOUT", "180.0"))

SENTRY_DSN: str | None = os.environ.get(
    "SENTRY_DSN",
    "https://8d8cab445edc9b4e452ba06d0be46dcb@sentry.archive.org/73",
)
SENTRY_TRACES_SAMPLE_RATE: float = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
SENTRY_PROFILE_SESSION_SAMPLE_RATE: float = float(os.environ.get("SENTRY_PROFILE_SESSION_SAMPLE_RATE", "0.1"))

FEATURED_SUBJECTS: list[dict[str, str]] = [
    {"key": "/subjects/art",                           "presentable_name": "Art"},
    {"key": "/subjects/science_fiction",               "presentable_name": "Science Fiction"},
    {"key": "/subjects/fantasy",                       "presentable_name": "Fantasy"},
    {"key": "/subjects/biographies",                   "presentable_name": "Biographies"},
    {"key": "/subjects/recipes",                       "presentable_name": "Recipes"},
    {"key": "/subjects/romance",                       "presentable_name": "Romance"},
    {"key": "/subjects/textbooks",                     "presentable_name": "Textbooks"},
    {"key": "/subjects/children",                      "presentable_name": "Children"},
    {"key": "/subjects/history",                       "presentable_name": "History"},
    {"key": "/subjects/medicine",                      "presentable_name": "Medicine"},
    {"key": "/subjects/religion",                      "presentable_name": "Religion"},
    {"key": "/subjects/mystery_and_detective_stories", "presentable_name": "Mystery and Detective Stories"},
    {"key": "/subjects/plays",                         "presentable_name": "Plays"},
    {"key": "/subjects/music",                         "presentable_name": "Music"},
    {"key": "/subjects/science",                       "presentable_name": "Science"},
    {"presentable_name": "Standard Ebooks",            "query": 'publisher:"Standard Ebooks" ebook_access:public'},
]

__all__ = [
    "OPDS_MEDIA_TYPE",
    "OPDS_PUB_MEDIA_TYPE",
    "OPDS_BASE_URL",
    "OL_BASE_URL",
    "OL_USER_AGENT",
    "OL_REQUEST_TIMEOUT",
    "FEATURED_SUBJECTS",
    "SENTRY_DSN",
    "ENVIRONMENT",
    "SENTRY_TRACES_SAMPLE_RATE",
    "SENTRY_PROFILE_SESSION_SAMPLE_RATE",
    "MEMCACHE_HOST",
    "MEMCACHE_PORT",
    "CACHE_ENABLED",
    "CORS_ENABLED",
]