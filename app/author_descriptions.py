"""An author's one-line description, looked up in a table shipped with the app.

Open Library shows a one-line summary under an author's portrait, e.g.
"French philosopher, author, and journalist (1913–1960)" for Camus. It is
the English description of the author's Wikidata item and is in no Open
Library API. Fetching it from wikidata.org would add a request to every
author page, so the service carries the whole table instead:
``app/data/author_descriptions.tsv.gz``, one ``OLID<TAB>description`` line
per author, built by ``scripts/build_author_descriptions.py`` (``make
author-descriptions``). A lookup here never touches the network; an author
missing from the table, because the link was made on Wikidata after the
table was built, simply has no description until the next build.

The table is held as the decompressed bytes plus two parallel arrays, the
OLID numbers in ascending order and each line's offset, and read by binary
search: about 15 MB per worker for 245,000 rows, against roughly three
times that for a ``dict`` of strings.
"""
from __future__ import annotations

import gzip
import re
import threading
from array import array
from bisect import bisect_left
from pathlib import Path
from typing import NamedTuple, Optional

from app.logger import get_logger

logger = get_logger(__name__)

TABLE_PATH = Path(__file__).resolve().parent / "data" / "author_descriptions.tsv.gz"

_OLID = re.compile(rb"^OL(\d+)A$")


class _Table(NamedTuple):
    numbers: array      # OLID numbers, ascending
    offsets: array      # byte offset of each line's description in ``text``
    ends: array         # byte offset just past each description
    text: bytes         # the decompressed TSV


def _parse(data: bytes) -> _Table:
    """Index the TSV in *data*; lines in any order, malformed lines skipped."""
    numbers, offsets, ends = array("Q"), array("Q"), array("Q")
    position = 0
    for line in data.splitlines(keepends=True):
        start, position = position, position + len(line)
        olid, tab, description = line.partition(b"\t")
        if not (tab and olid.startswith(b"OL") and olid.endswith(b"A")
                and olid[2:-1].isdigit() and description.strip()):
            continue
        numbers.append(int(olid[2:-1]))
        offsets.append(start + len(olid) + 1)
        ends.append(start + len(line.rstrip(b"\r\n")))
    if any(numbers[i] > numbers[i + 1] for i in range(len(numbers) - 1)):
        order = sorted(range(len(numbers)), key=numbers.__getitem__)
        numbers, offsets, ends = (array("Q", (a[i] for i in order)) for a in (numbers, offsets, ends))
    return _Table(numbers, offsets, ends, data)


_EMPTY = _Table(array("Q"), array("Q"), array("Q"), b"")
_table: Optional[_Table] = None
_lock = threading.Lock()


def load(path: Path = TABLE_PATH) -> _Table:
    """Read the table from *path*; an unreadable table is an empty one."""
    try:
        with gzip.open(path, "rb") as f:
            table = _parse(f.read())
    except Exception as exc:
        logger.error("author descriptions table %s unreadable: %s", path, exc)
        return _EMPTY
    logger.info("author descriptions table loaded: %d authors from %s", len(table.numbers), path)
    return table


def _get_table() -> _Table:
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = load()
    return _table


def reset(table: Optional[_Table] = None) -> None:
    """Replace the loaded table (tests), or drop it so the next lookup reloads."""
    global _table
    _table = table


def author_description(olid: str) -> Optional[str]:
    """The one-line description of author *olid*, or ``None`` when the table
    has none."""
    match = _OLID.match(olid.encode("ascii", "replace"))
    if not match:
        return None
    table = _get_table()
    number = int(match.group(1))
    index = bisect_left(table.numbers, number)
    if index == len(table.numbers) or table.numbers[index] != number:
        return None
    description = table.text[table.offsets[index]:table.ends[index]].decode("utf-8", "replace").strip()
    return description or None


def size() -> int:
    """How many authors the loaded table describes."""
    return len(_get_table().numbers)
