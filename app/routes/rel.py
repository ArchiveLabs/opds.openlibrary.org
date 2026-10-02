"""The link relations this feed coins, served at the URLs that name them.

A feed may coin a link relation of its own (RFC 8288 §2.1.2) as long as it
is a URL, and the courtesy is that the URL resolves to a page saying what the
relation means — ``http://opds-spec.org/acquisition/borrow`` resolves to the
OPDS specification. This feed's own relations, such as
``https://openlibrary.org/opds/rel/facet/availability``, are documented in
``app/docs/link-relations.md`` and served from here, one page for all of
them, so that the URL a client finds in a ``rel`` can be opened.

The relation strings are fixed, not built from the deployment's base URL: a
relation names a kind of link, the same on every host. What this route serves
is the documentation, wherever the feed is served from.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt

router = APIRouter()

REL_PREFIX = "https://openlibrary.org/opds/rel/"
_DOC = Path(__file__).resolve().parent.parent / "docs" / "link-relations.md"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open Library OPDS link relations</title>
<style>
  body {{ max-width: 42rem; margin: 2rem auto; padding: 0 1rem; font: 16px/1.5 system-ui, sans-serif; color: #222; }}
  code {{ font-size: 0.95em; }}
  h3 code, p > code:only-child {{ word-break: break-all; }}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def _slug(text: str) -> str:
    """``facet/media-type`` → ``facet-media-type``, an id a fragment can name."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@lru_cache(maxsize=1)
def _registry() -> tuple[str, frozenset[str]]:
    """The rendered page and the set of relation paths it documents.

    A relation is documented when its URL appears in the page as the
    ``https://openlibrary.org/opds/rel/<path>`` it is spelled with, so adding
    one to the markdown is all it takes to make its URL resolve.
    """
    source = _DOC.read_text(encoding="utf-8")
    md = MarkdownIt("commonmark")
    tokens = md.parse(source)
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            token.attrSet("id", _slug(tokens[i + 1].content))
    body = md.renderer.render(tokens, md.options, {})
    paths = frozenset(re.findall(re.escape(REL_PREFIX) + r"([a-z0-9/-]+)", source))
    return _PAGE.format(body=body), paths


@router.get("/rel", include_in_schema=False)
@router.get("/rel/", include_in_schema=False)
def rel_index() -> HTMLResponse:
    """Every relation this feed coins, on one page."""
    page, _ = _registry()
    return HTMLResponse(page)


@router.get("/rel/{path:path}", summary="What a link relation this feed coins means")
def rel_page(path: str) -> HTMLResponse:
    """The page a relation URL resolves to; 404 for a relation never coined."""
    page, paths = _registry()
    if path.rstrip("/") not in paths:
        raise HTTPException(status_code=404, detail=f"No such link relation: {REL_PREFIX}{path}")
    return HTMLResponse(page)
