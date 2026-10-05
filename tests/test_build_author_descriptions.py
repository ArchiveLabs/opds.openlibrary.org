"""The build script turns SPARQL TSV into the shipped table deterministically."""
from __future__ import annotations

import gzip
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_author_descriptions", REPO / "scripts" / "build_author_descriptions.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

TSV = "\n".join([
    "?olid\t?item\t?description",
    '"OL31353A"\t<http://www.wikidata.org/entity/Q181659>\t"American fantasy and science fiction author (1929–2018)"@en',
    '"OL1A"\t<http://www.wikidata.org/entity/Q500>\t"writer from\\tOdisha,\\nIndia"@en',
    '"OL5A"\t<http://www.wikidata.org/entity/Q9>\t"the \\"quoted\\" one"@en',
    '"OL5A"\t<http://www.wikidata.org/entity/Q3>\t"the lower item wins"@en',
    '"OL6A"\t<http://www.wikidata.org/entity/Q4>\t""@en',
    '"OL7W"\t<http://www.wikidata.org/entity/Q5>\t"a work, not an author"@en',
    "garbage line",
]) + "\n"


def test_rows_are_parsed_unescaped_deduplicated_and_filtered():
    table = build.parse_rows(TSV)
    assert table == {
        31353: "American fantasy and science fiction author (1929–2018)",
        1: "writer from Odisha, India",
        5: "the lower item wins",
    }


def test_rendering_is_sorted_by_olid_number_and_byte_for_byte_reproducible():
    table = build.parse_rows(TSV)
    data = build.render(table)
    assert data == build.render(dict(reversed(list(table.items()))))
    text = gzip.decompress(data).decode("utf-8")
    assert text == (
        "OL1A\twriter from Odisha, India\n"
        "OL5A\tthe lower item wins\n"
        "OL31353A\tAmerican fantasy and science fiction author (1929–2018)\n"
    )
