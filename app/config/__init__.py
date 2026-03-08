from __future__ import annotations


import os

OPDS_MEDIA_TYPE = "application/opds+json"
OPDS_PUB_MEDIA_TYPE = "application/opds-publication+json"

OPDS_BASE_URL: str | None = os.environ.get("OPDS_BASE_URL")
OL_BASE_URL: str = os.environ.get("OL_BASE_URL", "https://openlibrary.org")
OL_USER_AGENT: str = os.environ.get(
    "OL_USER_AGENT",
    "OPDSBot/1.0 (opds.openlibrary.org; opds@openlibrary.org)",
)
OL_REQUEST_TIMEOUT: float = float(os.environ.get("OL_REQUEST_TIMEOUT", "30.0"))

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
]

__all__ = [
    "OPDS_MEDIA_TYPE",
    "OPDS_PUB_MEDIA_TYPE",
    "OPDS_BASE_URL",
    "OL_BASE_URL",
    "OL_USER_AGENT",
    "OL_REQUEST_TIMEOUT",
    "FEATURED_SUBJECTS",
]