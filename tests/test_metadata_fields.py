"""Unit tests for new metadata fields (issues #93–#96)."""
from __future__ import annotations
import pytest
from pyopds2_openlibrary import OpenLibraryDataRecord, OpenLibraryDataProvider

OpenLibraryDataProvider.BASE_URL = "https://openlibrary.org"
OpenLibraryDataProvider.OPDS_BASE_URL = "https://opds.openlibrary.org/opds"


def _make_record(overrides: dict) -> OpenLibraryDataRecord:
    base = {
        "key": "/works/OL1W",
        "title": "Test Book",
        "editions": {
            "numFound": 1, "start": 0, "numFoundExact": True,
            "docs": [{"key": "/books/OL1M", "title": "Test Book"}],
        },
    }
    base.update(overrides)
    return OpenLibraryDataRecord.model_validate(base)


# ----- #96 cover images -----

def test_images_returns_large_and_medium_covers():
    rec = _make_record({"cover_i": 12345})
    imgs = rec.images()
    assert imgs is not None
    hrefs = [lnk.href for lnk in imgs]
    assert any("-L.jpg" in h for h in hrefs), "missing large cover"
    assert any("-M.jpg" in h for h in hrefs), "missing medium cover"


def test_images_both_covers_have_rel_cover():
    rec = _make_record({"cover_i": 12345})
    for lnk in rec.images():
        assert lnk.rel == "cover"


def test_images_none_when_no_cover():
    rec = _make_record({})
    assert rec.images() is None
