#!/usr/bin/env python3
"""Build ``app/data/author_descriptions.tsv.gz``: every Open Library author's
one-line Wikidata description.

Open Library shows a one-line summary under an author's portrait, e.g.
"French philosopher, author, and journalist (1913–1960)" for Camus. It is
the English description of the author's Wikidata item, and it is in no
Open Library API. Rather than ask wikidata.org for it on every author page,
the service ships the whole table: one SPARQL query lists the items that
carry an Open Library author ID (property P648) with their English
description, about 245,000 rows, 12 MB plain and 3.5 MB gzipped.

The public query.wikidata.org endpoint cannot answer it: it stops at 60
seconds, about halfway through. QLever's mirror of Wikidata (the
University of Freiburg's engine, at qlever.dev) answers in about 15
seconds, so that is the default endpoint; pass ``--endpoint`` to use
another SPARQL service.

The output is deterministic: rows are sorted by OLID number, an OLID that
several items claim keeps the lowest-numbered item's description, and the
gzip header carries no timestamp, so re-running without changes upstream
produces an identical file. Stdlib only, so it runs outside the app's venv:

    python3 scripts/build_author_descriptions.py
    make author-descriptions
"""
from __future__ import annotations

import argparse
import gzip
import io
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://qlever.dev/api/wikidata"
OUTPUT = Path(__file__).resolve().parent.parent / "app" / "data" / "author_descriptions.tsv.gz"
USER_AGENT = "opds.openlibrary.org author-descriptions build (opds@openlibrary.org)"

QUERY = """
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX schema: <http://schema.org/>
SELECT ?olid ?item ?description WHERE {
  ?item wdt:P648 ?olid .
  FILTER(STRENDS(?olid, "A"))
  ?item schema:description ?description .
  FILTER(LANG(?description) = "en")
}
"""

OLID = re.compile(r"^OL(\d+)A$")
ITEM = re.compile(r"Q(\d+)>?$")


def fetch_rows(endpoint: str) -> str:
    """The query's result as SPARQL TSV: one header line, then quoted terms."""
    body = urllib.parse.urlencode({"query": QUERY}).encode()
    request = urllib.request.Request(
        endpoint, data=body,
        headers={"Accept": "text/tab-separated-values", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read().decode("utf-8")


def _unquote(term: str) -> str:
    """The lexical form of a SPARQL TSV literal: ``"text"@en`` becomes ``text``.

    SPARQL TSV escapes a literal like Turtle does, so the backslash escapes
    are undone here; a tab or newline inside a description would break the
    plain TSV the service reads, so they become spaces.
    """
    if term.startswith('"'):
        term = term[1:term.rfind('"')]
    term = (term.replace("\\t", " ").replace("\\n", " ").replace("\\r", " ")
                .replace('\\"', '"').replace("\\\\", "\\"))
    return " ".join(term.split())


def parse_rows(tsv: str) -> dict[int, tuple[int, str]]:
    """``{olid number: (item number, description)}``, the lowest-numbered item
    winning a tie."""
    chosen: dict[int, tuple[int, str]] = {}
    lines = tsv.splitlines()
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        olid_match = OLID.match(_unquote(parts[0]))
        item_match = ITEM.search(parts[1])
        description = _unquote(parts[2])
        if not (olid_match and item_match and description):
            continue
        number, item = int(olid_match.group(1)), int(item_match.group(1))
        current = chosen.get(number)
        if current is None or item < current[0]:
            chosen[number] = (item, description)
    return chosen


def render(table: dict[int, tuple[int, str]]) -> bytes:
    """The gzipped TSV, ``OLID<TAB>Q…<TAB>description``, byte-for-byte
    reproducible for the same table. The item is kept so the feed can link
    the description to its source."""
    text = "".join(
        f"OL{number}A\tQ{table[number][0]}\t{table[number][1]}\n" for number in sorted(table)
    )
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(text.encode("utf-8"))
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=ENDPOINT, help="SPARQL endpoint (default: QLever's Wikidata mirror)")
    parser.add_argument("--output", type=Path, default=OUTPUT, help=f"where to write (default: {OUTPUT})")
    args = parser.parse_args(argv)

    print(f"querying {args.endpoint} …", file=sys.stderr)
    table = parse_rows(fetch_rows(args.endpoint))
    if len(table) < 100_000:
        print(f"only {len(table)} rows: refusing to overwrite {args.output}", file=sys.stderr)
        return 1
    data = render(table)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    print(f"wrote {len(table)} descriptions, {len(data)/1e6:.1f} MB, to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
