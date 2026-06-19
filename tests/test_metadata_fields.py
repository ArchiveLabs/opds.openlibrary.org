"""Unit tests for new metadata fields (issues #93–#96)."""
from __future__ import annotations

import pytest
import pyopds2_openlibrary as opds_module
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

def test_images_returns_three_responsive_sizes():
    rec = _make_record({"cover_i": 12345})
    imgs = rec.images()
    assert imgs is not None
    variants = {lnk.href.rsplit("-", 1)[1] for lnk in imgs}
    assert variants == {"L.jpg", "M.jpg", "S.jpg"}


def test_images_use_opds_spec_relations():
    rec = _make_record({"cover_i": 12345})
    rels = {lnk.href.rsplit("-", 1)[1]: lnk.rel for lnk in rec.images()}
    # Legacy OPDS 1.x rels on large + thumbnail; small is rel-less (OPDS 2.0
    # selects responsively).
    assert rels["L.jpg"] == "http://opds-spec.org/image"
    assert rels["M.jpg"] == "http://opds-spec.org/image/thumbnail"
    assert rels["S.jpg"] is None


def test_images_omit_width_and_height():
    rec = _make_record({"cover_i": 12345})
    for lnk in rec.images():
        d = lnk.model_dump()
        assert d.get("width") is None
        assert d.get("height") is None


def test_images_make_no_network_call(monkeypatch):
    rec = _make_record({"cover_i": 12345})

    def _boom(*args, **kwargs):
        raise AssertionError("images() must not perform any HTTP request")

    monkeypatch.setattr(opds_module.httpx, "get", _boom)
    assert rec.images() is not None


def test_displayed_cover_id_prefers_edition_then_work():
    rec = _make_record({"cover_i": 111})
    rec.editions.docs[0].cover_i = 222
    assert rec._displayed_cover_id() == 222
    rec.editions.docs[0].cover_i = None
    assert rec._displayed_cover_id() == 111


def test_images_none_when_no_cover():
    rec = _make_record({})
    assert rec.images() is None


# ----- #95 identifier -----
def test_identifier_set_from_isbn13():
    rec = _make_record({"isbn": ["0439708184", "9780439708180", "9780439708197"]})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("identifier") == "urn:isbn:9780439708180"


def test_identifier_skips_isbn10():
    rec = _make_record({"isbn": ["0439708184"]})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("identifier") is None


def test_identifier_absent_when_no_isbn():
    rec = _make_record({})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("identifier") is None


def test_identifier_rejects_non_isbn13_prefix():
    # 970-977 prefixes are GS1 but not valid ISBN-13
    rec = _make_record({"isbn": ["9700000000001", "9780439708180"]})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    # Should skip 970... and pick 978...
    assert dumped.get("identifier") == "urn:isbn:9780439708180"


# ----- #94 publisher + published -----

def test_publisher_name_from_work_level():
    rec = _make_record({"publisher": ["Scholastic", "Bloomsbury"]})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    pubs = dumped.get("publisher")
    assert pubs is not None and len(pubs) >= 1
    assert pubs[0]["name"] == "Scholastic"


def test_publisher_link_points_to_search():
    rec = _make_record({"publisher": ["Scholastic"]})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    link = dumped["publisher"][0]["links"][0]
    assert "Scholastic" in link["href"]
    assert link["type"] == "application/opds+json"


def test_published_year_from_first_publish_year():
    rec = _make_record({"first_publish_year": 1997})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    published = dumped.get("published")
    assert published is not None
    assert "1997" in str(published)


def test_publisher_absent_when_no_publisher():
    rec = _make_record({})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("publisher") is None


def test_published_absent_when_no_year():
    rec = _make_record({})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("published") is None


# ----- #93 belongsTo.series -----

def test_belongs_to_series_name_and_position():
    rec = _make_record({
        "series_name": ["Harry Potter"],
        "series_key": ["OL326110L"],
        "series_position": ["1"],
    })
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    bt = dumped.get("belongsTo")
    assert bt is not None
    series = bt.get("series")
    assert series is not None
    entry = series[0]
    assert entry["name"] == "Harry Potter"
    assert entry["position"] == 1.0


def test_belongs_to_series_link_type():
    rec = _make_record({
        "series_name": ["Discworld"],
        "series_key": ["OL999L"],
        "series_position": ["5"],
    })
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    entry = dumped["belongsTo"]["series"][0]
    link = entry["links"][0]
    assert link["type"] == "application/opds+json"
    assert "OL999L" in link["href"]


def test_belongs_to_multiple_series():
    rec = _make_record({
        "series_name": ["Discworld", "Rincewind"],
        "series_key": ["OL100L", "OL200L"],
        "series_position": ["15", "2"],
    })
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    series = dumped["belongsTo"]["series"]
    assert isinstance(series, list)
    assert len(series) == 2
    assert series[0]["name"] == "Discworld"
    assert series[1]["name"] == "Rincewind"


def test_belongs_to_absent_when_no_series():
    rec = _make_record({})
    meta = rec.metadata()
    dumped = meta.model_dump(by_alias=True)
    assert dumped.get("belongsTo") is None
