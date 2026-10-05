"""The author descriptions table: a one-line Wikidata description per
Open Library author, shipped with the app and looked up without a request.
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

import pytest

from app import author_descriptions
from app.author_descriptions import TABLE_PATH, author_description, load, reset, size

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def table(tmp_path):
    """A small table, unsorted and with a few malformed lines, loaded for the test."""
    lines = [
        "OL31353A\tAmerican fantasy and science fiction author (1929–2018)",
        "OL1A\twriter from Odisha, India",
        "not an olid\tignored",
        "OL7A",                      # no tab, no description
        "OL9A\t   ",                 # blank description
        "OL21594A\tEnglish novelist (1775–1817)",
    ]
    path = tmp_path / "t.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    reset(load(path))
    yield
    reset()


class TestLookup:
    def test_an_author_in_the_table_has_a_description(self, table):
        assert author_description("OL31353A") == "American fantasy and science fiction author (1929–2018)"
        assert author_description("OL1A") == "writer from Odisha, India"
        assert author_description("OL21594A") == "English novelist (1775–1817)"

    def test_an_author_missing_from_the_table_has_none(self, table):
        assert author_description("OL2A") is None
        assert author_description("OL99999999A") is None
        assert author_description("OL7A") is None
        assert author_description("OL9A") is None

    def test_malformed_lines_are_skipped_and_counted_out(self, table):
        assert size() == 3

    def test_something_that_is_not_an_olid_has_none(self, table):
        assert author_description("31353") is None
        assert author_description("OL31353W") is None
        assert author_description("") is None
        assert author_description("Ursula") is None

    def test_an_unreadable_table_is_an_empty_one(self, tmp_path):
        reset(load(tmp_path / "missing.tsv.gz"))
        try:
            assert size() == 0
            assert author_description("OL31353A") is None
        finally:
            reset()


class TestShippedTable:
    """The committed file is well formed, covers Open Library's authors
    broadly, and carries the author the e2e test looks at."""

    @pytest.fixture(autouse=True)
    def real_table(self):
        reset()
        yield
        reset()

    def test_it_is_where_the_app_looks_and_well_formed(self):
        assert TABLE_PATH == REPO / "app" / "data" / "author_descriptions.tsv.gz"
        with gzip.open(TABLE_PATH, "rt", encoding="utf-8") as f:
            lines = f.read().splitlines()
        assert len(lines) > 200_000
        olids = [line.split("\t", 1)[0] for line in lines]
        assert all(re.fullmatch(r"OL\d+A", olid) for olid in olids)
        numbers = [int(olid[2:-1]) for olid in olids]
        assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
        assert all(line.count("\t") == 1 and line.split("\t", 1)[1].strip() for line in lines)

    def test_it_is_reproducible_from_the_build_script(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build", REPO / "scripts" / "build_author_descriptions.py")
        build = importlib.util.module_from_spec(spec); spec.loader.exec_module(build)
        with gzip.open(TABLE_PATH, "rt", encoding="utf-8") as f:
            table = {int(olid[2:-1]): description for olid, description in (line.split("\t", 1) for line in f.read().splitlines())}
        assert build.render(table) == TABLE_PATH.read_bytes()

    def test_le_guin_is_described(self):
        assert author_description("OL31353A") == "American fantasy and science fiction author (1929–2018)"
        assert size() > 200_000
