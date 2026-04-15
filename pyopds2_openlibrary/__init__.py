import re as _re
import time as _time
import typing
from html.parser import HTMLParser as _HTMLParser
from typing import List, Optional, TypedDict, cast
from typing_extensions import Literal
from urllib.parse import urlencode

import httpx
from markdown_it import MarkdownIt as _MarkdownIt
from pydantic import BaseModel, Field

from pyopds2 import (
    DataProvider,
    DataProviderRecord,
    Contributor,
    Metadata,
    Link
)

_REQUEST_TIMEOUT: float = 30.0

# HTTP status codes that indicate a transient server-side failure worth retrying.
_RETRY_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
# Default delays between attempts (seconds): 3 total attempts — immediate, +1 s, +2 s.
# A 429 Retry-After header overrides the per-attempt delay when present.
_RETRY_DELAYS: tuple[float, ...] = (0.0, 1.0, 2.0)
# Cap on Retry-After to avoid holding a thread-pool thread for too long.
_RETRY_AFTER_MAX: float = 10.0


def _get(url: str, *, params=None, timeout: float = _REQUEST_TIMEOUT) -> httpx.Response:
    """``httpx.get`` with automatic retry on transient HTTP/network errors.

    Retries up to ``len(_RETRY_DELAYS) - 1`` times (default: 2 retries) for
    status codes in ``_RETRY_STATUS_CODES`` or for transport-level errors.
    Non-retryable HTTP errors (e.g. 404) are raised immediately.

    Respects the ``Retry-After`` response header on 429 replies, capped at
    ``_RETRY_AFTER_MAX`` seconds to avoid holding thread-pool threads too long.
    """
    delays = list(_RETRY_DELAYS)  # mutable copy so Retry-After can adjust future delays
    for i, delay in enumerate(delays):
        if delay:
            _time.sleep(delay)
        try:
            r = httpx.get(url, params=params, timeout=timeout)
            is_last = i == len(delays) - 1
            if r.status_code in _RETRY_STATUS_CODES and not is_last:
                # Honour Retry-After on 429; overwrite the *next* scheduled delay.
                if r.status_code == 429:
                    try:
                        delays[i + 1] = min(float(r.headers.get("Retry-After", "")), _RETRY_AFTER_MAX)
                    except (ValueError, IndexError):
                        pass
                continue
            r.raise_for_status()
            return r
        except httpx.TransportError:
            if i == len(delays) - 1:
                raise
    # All retries exhausted — raise_for_status surfaces the last response error.
    r.raise_for_status()
    return r  # unreachable; satisfies type checker



class BookSharedDoc(BaseModel):
    """Fields shared between OpenLibrary works and editions."""
    key: Optional[str] = None
    title: Optional[str] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    cover_i: Optional[int] = None
    ebook_access: Optional[str] = None
    language: Optional[list[str]] = None
    ia: Optional[list[str]] = None


class OpenLibraryDataRecord(BookSharedDoc, DataProviderRecord):

    class EditionAvailability(BaseModel):
        status: Literal["borrow_available", "borrow_unavailable",  "open", "private", "error"]

    class EditionProvider(BaseModel):
        """Basically the acquisition info for an edition."""
        access: Optional[str] = None
        format: Optional[Literal['web', 'pdf', 'epub', 'audio']] = None
        price: Optional[str] = None
        """Book price, eg '0.00 USD'"""
        url: Optional[str] = None
        provider_name: Optional[str] = None

    class EditionDoc(BookSharedDoc):
        """Open Library edition document."""
        availability: Optional["OpenLibraryDataRecord.EditionAvailability"] = None
        providers: Optional[list["OpenLibraryDataRecord.EditionProvider"]] = None

    class EditionsResultSet(BaseModel):
        numFound: Optional[int] = None
        start: Optional[int] = None
        numFoundExact: Optional[bool] = None
        docs: Optional[list["OpenLibraryDataRecord.EditionDoc"]] = None

    author_key: Optional[list[str]] = Field(
        None, description="List of author keys"
    )
    author_name: Optional[list[str]] = Field(
        None, description="List of author names"
    )
    editions: Optional["OpenLibraryDataRecord.EditionsResultSet"] = Field(
        None, description="Editions information (nested structure)"
    )
    number_of_pages_median: Optional[int] = None

    @property
    def type(self) -> str:
        """Type _should_ be improved to dynamically return type based on record data."""
        return "http://schema.org/Book"

    def links(self) -> List[Link]:
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self
        opds_base = OpenLibraryDataProvider.OPDS_BASE_URL or f"{OpenLibraryDataProvider.BASE_URL}/opds"

        links: list[Link] = [
            Link(
                rel="self",
                href=f"{opds_base}{book.key}",
                type="application/opds-publication+json",
            ),
            Link(
                rel="alternate",
                href=f"{OpenLibraryDataProvider.BASE_URL}{book.key}",
                type="text/html",
            ),
            Link(
                rel="alternate",
                href=f"{OpenLibraryDataProvider.BASE_URL}{book.key}.json",
                type="application/json",
            ),
        ]

        if not edition or not edition.providers:
            return links

        seen: set[tuple[str, str | None]] = set()
        for acquisition in edition.providers:
            if not acquisition.url:
                continue
            for link in ol_acquisition_to_opds_links(edition, acquisition):
                key = (link.href, link.type)
                if key not in seen:
                    seen.add(key)
                    links.append(link)
        return links

    def images(self) -> Optional[List[Link]]:
        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self
        if book.cover_i:
            return [
                Link(href=f"https://covers.openlibrary.org/b/id/{book.cover_i}-L.jpg", type="image/jpeg", rel="cover"),
            ]
        return None

    def metadata(self) -> Metadata:
        """Return this record as OPDS Metadata."""
        def get_authors() -> Optional[List[Contributor]]:
            if self.author_name and self.author_key:
                return [
                    Contributor(
                        name=name,
                        links=[
                            Link(
                                href=f"{OpenLibraryDataProvider.BASE_URL}/authors/{key}",
                                type="text/html",
                                rel="author"
                            )
                        ]
                    )
                    for name, key in zip(self.author_name, self.author_key)
                ]
            if self.author_name:
                return [Contributor(name=name) for name in self.author_name]

        edition = self.editions.docs[0] if self.editions and self.editions.docs else None
        book = edition or self

        return Metadata(
            type=self.type,
            title=book.title or self.title or "Untitled",
            subtitle=book.subtitle,
            author=get_authors(),
            description=strip_markdown(book.description or self.description) if (book.description or self.description) else None,
            language=[lang for marc_lang in (book.language or []) if (lang := marc_language_to_iso_639_1(marc_lang))],
            # TODO: Use the edition-specific pagecount
            numberOfPages=self.number_of_pages_median,
        )


class OpenLibraryLanguageStub(TypedDict):
    key: str
    identifiers: dict[str, list[str]] | None


# Non-IA provider formats that produce acquisition links.
# epub and pdf are direct downloads.  "web" is normally a plain website link
# and is excluded — except when access="buy", which indicates a purchase link
# to a bookstore (e.g. Better World Books).  "audio" is omitted because
# audio providers (e.g. Librivox) are already surfaced via the IA webpub link.
_DOWNLOADABLE_FORMATS: frozenset[str] = frozenset({"epub", "pdf"})



def _build_ia_alternate_link(edition: OpenLibraryDataRecord.EditionDoc) -> Link:
    """Build an alternate link for an Internet Archive provider.

    Produces a ``rel=alternate`` link to the IA webpub manifest endpoint.
    An ``authenticate`` property tells the client where to obtain credentials.

    Link schema: https://github.com/readium/webpub-manifest/blob/master/schema/link.schema.json
    """
    if not edition.ia:
        raise ValueError("edition.ia must be non-empty to build an IA alternate link")
    identifier = edition.ia[0]
    return Link(
        title="Internet Archive",
        href=f"https://archive.org/services/loans/loan/?action=webpub&identifier={identifier}&opds=1",
        rel="alternate",
        type="application/opds-publication+json",
        properties={
            "authenticate": {
                "href": "https://archive.org/services/loans/loan/?action=authentication_document",
                "type": "application/opds-authentication+json",
            }
        },
    )


def _build_external_acquisition_link(
    edition: OpenLibraryDataRecord.EditionDoc,
    acq: OpenLibraryDataRecord.EditionProvider,
) -> Link:
    """Build an acquisition link for a non-IA provider (e.g. Standard Ebooks).

    ``availability`` is only set for borrow/loan links — purchase links
    (``access="buy"``) are independent of the edition's loan state.

    ``indirectAcquisition`` is only set for downloadable formats (epub, pdf)
    where the link leads to a file rather than a web page.  Setting it on
    web purchase links is circular (``text/html`` → ``text/html``) and
    confuses OPDS clients.
    """
    rel = f'http://opds-spec.org/acquisition/{acq.access}' if acq.access else 'http://opds-spec.org/acquisition'
    if edition.ebook_access == "public":
        rel = "http://opds-spec.org/acquisition/open-access"

    link = Link(
        href=acq.url,
        rel=rel,
        type=map_ol_format_to_mime(acq.format) if acq.format else None,
        properties={}
    )

    # availability reflects loan/access state — not meaningful for purchase links.
    if acq.access != "buy":
        if edition.ebook_access:
            link.properties["availability"] = "unavailable"
            if edition.ebook_access == "public":
                link.properties["availability"] = "available"
        # availability.status represents loan state (checked in/out) — only applies to
        # borrowable books. Public/open-access books are always available regardless of
        # loan state, so we skip this block for them to avoid overriding the correct value.
        if edition.availability and edition.ebook_access != "public":
            status = edition.availability.status
            if status == "open" or status == "borrow_available":
                link.properties["availability"] = "available"
            elif status in ("private", "error", "borrow_unavailable"):
                link.properties["availability"] = "unavailable"

    if acq.provider_name:
        link.title = acq.provider_name
        # indirectAcquisition describes a DRM acquisition chain (e.g. ACSM → epub).
        # Only set it for downloadable formats — not for web purchase pages where
        # the type would be text/html → text/html (circular and meaningless).
        if acq.format in _DOWNLOADABLE_FORMATS:
            link.properties["indirectAcquisition"] = [
                {
                    "type": link.type,
                    "title": acq.provider_name,
                }
            ]

    if acq.price:
        amount = _parse_price_amount(acq.price)
        price_parts = acq.price.split(maxsplit=1)
        currency = price_parts[1] if len(price_parts) > 1 else None
        if amount is not None and currency:
            link.properties["price"] = {
                "value": amount,
                "currency": currency,
            }

    return link


def ol_acquisition_to_opds_links(
    edition: OpenLibraryDataRecord.EditionDoc,
    acq: OpenLibraryDataRecord.EditionProvider,
) -> List[Link]:
    """Convert an OL provider into zero or more OPDS links.

    IA providers → webpub alternate link (consistent reader UX).
    epub / pdf → acquisition download link.
    web + access="buy" → purchase link to an external bookstore.
    other web / audio → omitted (generic website links or Librivox audio
        already covered by the IA webpub alternate).
    """
    if not acq.url:
        raise ValueError("Provider URL is required for acquisition links")

    if acq.provider_name == "ia" and edition.ia:
        # All IA providers for the same edition share the same webpub URL.
        # Deduplication in links() (keyed on href+type) keeps only one copy.
        return [_build_ia_alternate_link(edition)]

    # Include web-format links only when they are explicit purchase links
    # (access="buy").  Generic web links (read online, browse) are excluded.
    if acq.format == "web" and acq.access != "buy":
        return []

    if acq.format not in _DOWNLOADABLE_FORMATS and acq.format != "web":
        return []

    return [_build_external_acquisition_link(edition, acq)]


def map_ol_format_to_mime(ol_format: Literal['web', 'pdf', 'epub', 'audio', 'daisy', 'djvu', 'mobi', 'txt'] | str) -> Optional[str]:
    """Map Open Library format strings to MIME types."""
    mapping = {
        'web': 'text/html',
        'pdf': 'application/pdf',
        'epub': 'application/epub+zip',
        'audio': 'audio/mpeg',
        'daisy': 'application/daisy+zip',
        'djvu': 'image/vnd.djvu',
        'mobi': 'application/x-mobipocket-ebook',
        'txt': 'text/plain',
    }
    return mapping.get(ol_format, 'application/octet-stream')


class _HTMLStripper(_HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


_md = _MarkdownIt()


def strip_markdown(text: str) -> str:
    """Convert Markdown/HTML to plain text using markdown-it-py.

    OpenLibrary descriptions may contain Markdown links, horizontal rules,
    emphasis, headings, and occasional inline HTML.  This function strips
    all of that to produce clean readable text suitable for OPDS 2.0
    ``description`` fields.
    """
    html = _md.render(text)
    stripper = _HTMLStripper()
    stripper.feed(html)
    result = stripper.get_text()
    result = result.replace('\r\n', '\n')
    result = _re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


def marc_language_to_iso_639_1(marc_code: str) -> Optional[str]:
    """Convert a MARC language code to an ISO 639-1 code using the cached languages map.

    Returns ``None`` (rather than raising) if the languages map cannot be fetched,
    so that book metadata is still returned without a language field on OL API errors.
    """
    try:
        return fetch_languages_map().get(marc_code)
    except Exception:
        return None


_languages_map_cache: Optional[dict[str, str]] = None
_languages_map_fetched_at: float = 0.0
_LANGUAGES_MAP_TTL: float = 24 * 60 * 60  # 1 day


def fetch_languages_map() -> dict[str, str]:
    """Return a map of MARC language codes to ISO 639-1 codes.

    Results are cached for ``_LANGUAGES_MAP_TTL`` seconds.  Unlike
    ``@functools.cache``, a failure to fetch does **not** poison the cache —
    the next request will retry the OL API rather than returning stale ``{}``.
    """
    global _languages_map_cache, _languages_map_fetched_at
    now = _time.monotonic()
    if _languages_map_cache is not None and (now - _languages_map_fetched_at) < _LANGUAGES_MAP_TTL:
        return _languages_map_cache
    try:
        r = _get("https://openlibrary.org/query.json?type=/type/language&key&identifiers&limit=1000")
    except Exception:
        if _languages_map_cache is not None:
            return _languages_map_cache
        raise
    data = cast(List[OpenLibraryLanguageStub], r.json())
    languages: dict[str, str] = {}
    for lang in data:
        marc_code = lang["key"].split("/")[-1]
        identifiers = lang.get("identifiers")
        if not identifiers:
            continue
        iso_codes = identifiers.get("iso_639_1", [])
        if iso_codes:
            languages[marc_code] = iso_codes[0]
    _languages_map_cache = languages
    _languages_map_fetched_at = now
    return languages


def _has_acquisition_options(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition would produce at least one usable OPDS link.

    Mirrors the filtering logic in ``ol_acquisition_to_opds_links`` so that
    books with no actionable link are hidden from results.
    """
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition or not edition.providers:
        return False
    for p in edition.providers:
        if not p.url:
            continue
        # Any IA provider produces a webpub alternate link.
        if p.provider_name == "ia" and edition.ia:
            return True
        # epub/pdf → download link.
        if p.format in _DOWNLOADABLE_FORMATS:
            return True
        # web + access="buy" → purchase link to an external bookstore.
        if p.format == "web" and p.access == "buy":
            return True
    return False


def _has_cover(record: OpenLibraryDataRecord) -> bool:
    """Check if a record has a cover image at the edition or work level."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if edition and edition.cover_i:
        return True
    return bool(record.cover_i)


def _is_currently_available(record: OpenLibraryDataRecord) -> bool:
    """Check if a record's edition is currently available (not checked out)."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition:
        return False
    if edition.ebook_access == "public":
        return True
    if edition.availability and edition.availability.status == "borrow_unavailable":
        return False
    return True


def _parse_price_amount(price: str) -> Optional[float]:
    """Parse the leading numeric portion of a price string (e.g. '0.99 USD') into a float.

    Returns None if parsing fails.
    """
    if not price:
        return None
    numeric_part = price.split(maxsplit=1)[0]
    try:
        return float(numeric_part)
    except ValueError:
        return None


def _has_buyable_provider(record: OpenLibraryDataRecord) -> bool:
    """Check if a record has at least one provider with a non-zero price."""
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if not edition or not edition.providers:
        return False
    for p in edition.providers:
        if not p.price:
            continue
        amount = _parse_price_amount(p.price)
        if amount is not None and amount > 0:
            return True
    return False


def _get_edition_ebook_access(record: OpenLibraryDataRecord) -> Optional[str]:
    """Return the edition-level ebook_access, falling back to the work-level value.

    Used by the availability post-filter to enforce mode boundaries after
    language-based edition resolution may have swapped in a different edition.
    """
    edition = record.editions.docs[0] if record.editions and record.editions.docs else None
    if edition and edition.ebook_access:
        return edition.ebook_access
    return record.ebook_access


# Strict allowlist of edition-level ebook_access values per availability mode.
# The Solr query for 'ebooks' uses a range that includes 'public'; this post-filter
# enforces the correct boundary after all edition resolution is complete.
# 'buyable' is intentionally excluded: it is filtered client-side via
# _has_buyable_provider, and an open-access book with a priced provider is a valid result.
_EBOOK_MODE_ALLOWED: dict[str, frozenset[str]] = {
    "ebooks":      frozenset({"borrowable", "printdisabled"}),
    "open_access": frozenset({"public"}),
}

_EBOOK_ACCESS_RANK = {
    "public": 3,
    "borrowable": 2,
    "printdisabled": 1,
    "no_ebook": 0,
}


def _ebook_access_rank(ebook_access: Optional[str]) -> int:
    """Return a numeric rank for an ebook_access value (higher = more accessible)."""
    return _EBOOK_ACCESS_RANK.get(ebook_access or "no_ebook", 0)


class _ResolvedEdition(typing.NamedTuple):
    """Result of _resolve_preferred_edition."""
    edition: "OpenLibraryDataRecord.EditionDoc"
    author_name: Optional[list[str]]
    author_key: Optional[list[str]]


def _resolve_preferred_edition(
    work_key: str,
    language: str,
    edition_fields: list[str],
) -> Optional["_ResolvedEdition"]:
    """Find a full EditionDoc for *work_key* in the preferred MARC language code.

    Makes up to two requests:
    1. Work editions endpoint to find an edition key matching *language*.
    2. Search endpoint to get the full edition data (providers, availability, etc.).

    Returns a ``_ResolvedEdition`` namedtuple (edition + author data) or
    ``None`` if no matching edition is found or any request fails.
    """
    try:
        r = _get(
            f"{OpenLibraryDataProvider.BASE_URL}{work_key}/editions.json",
            params={"limit": 50, "fields": "key,languages"},
        )
        preferred_olid: Optional[str] = None
        for entry in r.json().get("entries", []):
            langs = [lang["key"].split("/")[-1] for lang in entry.get("languages", [])]
            if language in langs:
                # entry["key"] is like "/books/OL27945116M"
                preferred_olid = entry["key"].split("/")[-1]
                break

        if not preferred_olid:
            return None

        # Include author fields so we can fix author name for the preferred edition.
        search_fields = "editions,author_name,author_key," + ",".join(edition_fields)
        r2 = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={
                "q": f"edition_key:{preferred_olid}",
                "editions": "true",
                "fields": search_fields,
                "limit": 1,
            },
        )
        docs = r2.json().get("docs", [])
        if not docs:
            return None
        edition_docs = docs[0].get("editions", {}).get("docs", [])
        if not edition_docs:
            return None
        edition = OpenLibraryDataRecord.EditionDoc.model_validate(edition_docs[0])
        author_name = docs[0].get("author_name") or None
        author_key = docs[0].get("author_key") or None
        return _ResolvedEdition(edition=edition, author_name=author_name, author_key=author_key)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared availability-facet primitives
# ---------------------------------------------------------------------------

# Single canonical label per mode — used by both build_facets and build_home_facets.
_AVAILABILITY_MODES: list[tuple[str, str]] = [
    ("everything",  "Everything"),
    ("ebooks",      "Available to Borrow"),
    ("open_access", "Open Access"),
    ("buyable",     "Available for Purchase"),
]

# Language options for the Language facet group (OPDS 2.0 §2.4).
# ``None`` means "no language filter" (All Languages).
# Language codes follow BCP 47 / ISO 639-1 (e.g. "en" for English).
_LANGUAGE_OPTIONS: list[tuple[Optional[str], str]] = [
    (None, "All"),
    ("en", "English"),
]


def _build_availability_links(
    mode: str,
    href_fn: typing.Callable[[str], str],
    labels: Optional[dict[str, str]] = None,
    counts: Optional[dict[str, Optional[int]]] = None,
    exclude: Optional[set[str]] = None,
) -> list[dict]:
    """Build the list of availability facet link dicts (single implementation).

    Each caller supplies its own ``href_fn`` (Open/Closed) so this function
    never needs to change when a new page type needs availability facets.

    Args:
        mode: Currently active mode value (e.g. ``"ebooks"``).
        href_fn: Converts a mode value string to a full URL.
        labels: ``{mode_value: display_label}`` overrides.  Unspecified modes
            fall back to the search-page label in ``_AVAILABILITY_MODES``.
        counts: ``{mode_value: item_count}`` for ``numberOfItems`` (OPDS 2.0 §2.4).
        exclude: Mode values to omit from the facet list.
    """
    default_labels = {val: label for val, label in _AVAILABILITY_MODES}
    resolved = {**default_labels, **(labels or {})}
    counts = counts or {}
    exclude = exclude or set()
    links = []
    for val, _ in _AVAILABILITY_MODES:
        if val in exclude:
            continue
        link: dict = {
            "title": resolved[val],
            "href": href_fn(val),
            "type": "application/opds+json",
        }
        if val == mode:
            link["rel"] = "self"
        count = counts.get(val)
        if count is not None:
            link.setdefault("properties", {})["numberOfItems"] = count
        links.append(link)
    return links


def _build_language_links(
    language: Optional[str],
    href_fn: typing.Callable[[Optional[str]], str],
) -> list[dict]:
    """Build the list of language facet link dicts per OPDS 2.0 §2.4.

    Iterates ``_LANGUAGE_OPTIONS`` so adding a new language only requires
    appending to that list — no logic changes needed here.

    The currently active language is indicated by ``rel: "self"`` on its link,
    as required by the OPDS 2.0 specification.  ``language=None`` means
    "All Languages" (no filter); that entry is always first in the list.

    Args:
        language: Active BCP 47 language code (e.g. ``"en"``), or ``None``
            for the "All Languages" (unfiltered) selection.
        href_fn: Converts a language code (or ``None``) to a full URL.
    """
    links = []
    for lang_code, label in _LANGUAGE_OPTIONS:
        link: dict = {
            "title": label,
            "href": href_fn(lang_code),
            "type": "application/opds+json",
        }
        if lang_code == language:
            link["rel"] = "self"
        links.append(link)
    return links

class OpenLibraryDataProvider(DataProvider):
    """Data provider for Open Library records."""
    BASE_URL: str = "https://openlibrary.org"
    OPDS_BASE_URL: Optional[str] = None
    TITLE: str = "OpenLibrary.org OPDS Service"
    SEARCH_URL: str = "/opds/search{?query}"

    @classmethod
    def bookshelf_link(cls, host="https://archive.org"):
        return Link(
            rel="http://opds-spec.org/shelf",
            href=f"{host}/services/loans/loan/?action=user_bookshelf",
            type="application/opds+json",
        )

    @classmethod
    def profile_link(cls, host="https://archive.org"):
        return Link(
            rel="profile",
            href=f"{host}/services/loans/loan/?action=user_profile",
            type="application/opds-profile+json",
        )

    @staticmethod
    def _count_for_mode(query: str, mode: str) -> Optional[int]:
        """Run a lightweight ``limit=0`` search to get the total count for a mode.

        Returns ``None`` for modes that require client-side filtering (like
        ``buyable``) since Solr cannot provide an accurate count.
        """
        if mode == 'buyable':
            # Buyable is filtered client-side (_has_buyable_provider); Solr
            # has no field for it so we cannot produce an accurate count.
            return None

        internal_query = query
        if mode == 'ebooks' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"

        r = _get(
            f"{OpenLibraryDataProvider.BASE_URL}/search.json",
            params={"q": internal_query, "limit": 0, "fields": "key"},
        )
        return r.json().get("numFound", 0)

    @staticmethod
    def fetch_facet_counts(query: str, known_mode: Optional[str] = None, known_total: Optional[int] = None) -> dict[str, Optional[int]]:
        """Fetch ``numberOfItems`` counts for every availability mode.

        If *known_mode* and *known_total* are provided the count request for
        that mode is skipped (we already have it from the main search).

        Modes that cannot be counted server-side (e.g. ``buyable``) will have
        a ``None`` value unless supplied via *known_mode*/*known_total*.
        """
        modes = ["everything", "ebooks", "open_access", "buyable"]
        counts: dict[str, Optional[int]] = {}
        for m in modes:
            if known_mode and m == known_mode and known_total is not None:
                counts[m] = known_total
            else:
                counts[m] = OpenLibraryDataProvider._count_for_mode(query, m)
        return counts

    @staticmethod
    def build_facets(
        base_url: str,
        query: str,
        sort: Optional[str] = None,
        mode: str = "everything",
        language: Optional[str] = None,
        title: Optional[str] = None,
        total: Optional[int] = None,
        availability_counts: Optional[dict[str, int]] = None,
    ) -> list[dict]:
        """Build OPDS 2.0 facets for availability and language filtering.

        Returns two facet groups per OPDS 2.0 §2.4:
        - **Availability** – links to ``/search`` with the appropriate mode.
        - **Language** – links to ``/search`` with the appropriate language code.

        Each active facet link is marked with ``rel: "self"`` as required by
        the specification.  Availability links point to ``/search``; for
        homepage facets use ``build_home_facets``.

        The Sort group is retained in ``_build_sort_links`` but not included here.
        To re-enable sort: add the Sort group dict to the returned list.

        Args:
            language: BCP 47 language code for the active selection (e.g.
                ``"en"``), or ``None`` for "All Languages" (no filter).
                This value is preserved in every facet href so that switching
                availability mode keeps the current language selection, and
                vice-versa.
            title: Display title for the search results page (e.g. ``"Art"``).
                Preserved in every facet href so switching facets keeps the
                page title instead of falling back to "Search Results".
            total: Reserved for Sort links when re-enabled.
            availability_counts: ``{mode_value: item_count}`` per OPDS 2.0 §2.4.
        """
        def search_href(
            sort_val: Optional[str] = sort,
            mode_val: str = mode,
            lang_val: Optional[str] = language,
        ) -> str:
            params: dict[str, str] = {"query": query}
            if sort_val:
                params["sort"] = sort_val
            if mode_val and mode_val != "everything":
                params["mode"] = mode_val
            if lang_val:
                params["language"] = lang_val
            if title:
                params["title"] = title
            return f"{base_url}/search?{urlencode(params)}"

        # Sort group is unplugged but preserved — re-enable by adding:
        # {"metadata": {"title": "Sort"},
        #  "links": _build_sort_links(sort, lambda sv: search_href(sort_val=sv), total)}

        return [
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=lambda val: search_href(mode_val=val),
                    counts=availability_counts,
                ),
            },
            {
                "metadata": {"title": "Language"},
                "links": _build_language_links(
                    language=language,
                    href_fn=lambda lang: search_href(lang_val=lang),
                ),
            },
        ]

    @staticmethod
    def build_home_facets(
        base_url: str,
        mode: str = "everything",
        language: Optional[str] = None,
    ) -> list[dict]:
        """Build Availability and Language facet groups for the OPDS homepage.

        Uses the same canonical labels as ``build_facets``.
        Links point to ``<base_url>/?mode=<value>`` / ``<base_url>/?language=<code>``.
        Each active facet link is marked with ``rel: "self"`` per OPDS 2.0 §2.4.

        Args:
            base_url: Base URL of the OPDS service (no trailing slash).
            mode: Currently active availability mode.
            language: Active BCP 47 language code (e.g. ``"en"``), or ``None``
                for "All Languages" (no filter).
        """
        def home_href(val: str) -> str:
            params: dict[str, str] = {}
            if val != "everything":
                params["mode"] = val
            if language:
                params["language"] = language
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        def lang_href(lang: Optional[str]) -> str:
            params: dict[str, str] = {}
            if mode != "everything":
                params["mode"] = mode
            if lang:
                params["language"] = lang
            return f"{base_url}/?{urlencode(params)}" if params else f"{base_url}/"

        return [
            {
                "metadata": {"title": "Availability"},
                "links": _build_availability_links(
                    mode=mode,
                    href_fn=home_href,
                    exclude={"buyable"},
                ),
            },
            {
                "metadata": {"title": "Language"},
                "links": _build_language_links(
                    language=language,
                    href_fn=lang_href,
                ),
            },
        ]

    @typing.override
    @staticmethod
    def search(
        query: str,
        limit: int = 50,
        offset: int = 0,
        sort: Optional[str] = None,
        facets: Optional[dict[str, str]] = None,
        language: Optional[str] = None,
        title: Optional[str] = None,
    ) -> DataProvider.SearchResponse:
        """
        Search Open Library.

        Args:
            query: The search query string.
            limit: Maximum number of results to return.
            offset: Number of results to skip.
            sort: Sort order for results.
            facets: Optional facets to apply. Supported facets:
                - 'mode':
                    * 'everything' (default): return all matching results
                      with no ebook filter.
                    * 'ebooks': filter to records with any ebook access
                      (``ebook_access:[printdisabled TO *]``), then hide
                      records without acquisition options.
                    * 'open_access': filter to open-access/public ebooks
                      (``ebook_access:public``), then hide records without
                      acquisition options.
                    * 'buyable': filter to records with ebook access
                      (``ebook_access:[printdisabled TO *]``), then hide
                      records without acquisition options and keep only
                      those that have at least one non-free provider.
            language: BCP 47 language code to prefer (e.g. ``"en"`` for
                English), or ``None`` to return results in all languages
                without any language filter.  When set, OL is asked to
                surface editions in that language and, for
                ``edition_key:`` queries, the preferred edition is
                resolved via the work's editions endpoint.
        """
        fields = [
            "key", "title", "editions", "description", "providers", "author_name", "ia",
            "cover_i", "availability", "ebook_access", "author_key", "subtitle", "language",
            "number_of_pages_median",
        ]

        internal_query = query
        if facets:
            mode = facets.get('mode', 'everything')
        else:
            mode = 'everything'

        if mode == 'ebooks' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"
        elif mode == 'open_access' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:public"
        elif mode == 'buyable' and 'ebook_access:' not in internal_query:
            internal_query = f"{internal_query} ebook_access:[printdisabled TO *]"

        params = {
            "editions": "true",
            "q": internal_query,
            "page": (offset // limit) + 1 if limit else 1,
            "limit": limit,
            **({'sort': sort} if sort else {}),
            "fields": ",".join(fields),
            # Ask OL to prefer editions in the requested language.
            # This works for general queries but is ignored when edition_key: is present.
            **({'lang': language} if language else {}),
        }
        r = _get(f"{OpenLibraryDataProvider.BASE_URL}/search.json", params=params)
        data = r.json()
        docs = data.get("docs", [])
        records = []
        for doc in docs:
            # Unpack editions field if present
            if "editions" in doc and isinstance(doc["editions"], dict):
                doc = dict(doc)
                doc["editions"] = OpenLibraryDataRecord.EditionsResultSet.model_validate(doc["editions"])
            records.append(OpenLibraryDataRecord.model_validate(doc))

        total = data.get("numFound", 0)

        # When the query targets a specific edition (edition_key:), OL ignores the
        # lang param and always returns that edition.  If its language doesn't match
        # the preference, resolve the correct edition from the work.
        if language and "edition_key:" in query:
            edition_fields = [
                "key", "title", "subtitle", "description", "cover_i",
                "ebook_access", "language", "ia", "availability", "providers",
            ]
            for record in records:
                if record.editions and record.editions.docs:
                    ed = record.editions.docs[0]
                    if not ed.language or language not in ed.language:
                        preferred = _resolve_preferred_edition(
                            record.key or "", language, edition_fields
                        )
                        # Always prefer the language-matched edition over a foreign-language
                        # one, even if its ebook-access rank is lower. Language
                        # correctness takes priority; the user can still follow
                        # the alternate link to the original edition.
                        if preferred:
                            record.editions.docs[0] = preferred.edition
                            # Fix author name/key if the edition-level search
                            # returned localised author data.
                            if preferred.author_name:
                                record.author_name = preferred.author_name
                            if preferred.author_key:
                                record.author_key = preferred.author_key

        # When OL returns multiple editions, move language-matching ones first
        # (fallback for cases where lang param alone isn't sufficient).
        if language:
            for record in records:
                if record.editions and record.editions.docs and len(record.editions.docs) > 1:
                    matched = [d for d in record.editions.docs if d.language and language in d.language]
                    others = [d for d in record.editions.docs if not (d.language and language in d.language)]
                    if matched:
                        record.editions.docs = matched + others

        # Always filter out records with no usable OPDS links or no cover image.
        # - No acquisition options: the user has nothing to tap in the reader app.
        # - No cover: produces a broken "Cover Unavailable" card that degrades UX.
        records = [r for r in records if _has_acquisition_options(r) and _has_cover(r)]

        if mode in ('ebooks', 'open_access', 'buyable'):
            if mode == 'buyable':
                # Keep only records that have a non-free provider.
                # This is a client-side filter (Solr has no buyable field).
                records = [r for r in records if _has_buyable_provider(r)]
            # Sort available books before unavailable, preserving order within each group
            records.sort(key=lambda r: (0 if _is_currently_available(r) else 1))

        # Strict post-filter: enforce ebook_access boundaries after all edition resolution.
        # We check BOTH edition-level AND work-level so that language-based edition swaps
        # cannot cause false negatives. Example: a public-domain work (work.ebook_access=
        # "public") whose language-preferred edition happens to have ebook_access=
        # "borrowable" should still appear in open_access mode — the Solr query already
        # guaranteed the work is public. For ebooks mode, the work-level OR means a
        # borrowable work whose language-swapped edition is public will still be included.
        if mode in _EBOOK_MODE_ALLOWED:
            allowed = _EBOOK_MODE_ALLOWED[mode]
            records = [r for r in records if _get_edition_ebook_access(r) in allowed or r.ebook_access in allowed]

        # Set total for buyable after ALL filters — client-side filtering means Solr
        # cannot produce an accurate count; use the final post-filter record count.
        if mode == 'buyable':
            total = len(records)

        response_kwargs = {
            "provider": OpenLibraryDataProvider,
            "records": records,
            "total": total,
            "query": query,
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }
        # Some pyopds2 versions include title in SearchResponse, some do not.
        if title is not None and "title" in getattr(DataProvider.SearchResponse, "__dataclass_fields__", {}):
            response_kwargs["title"] = title

        resp = DataProvider.SearchResponse(**response_kwargs)

        # Backward-compatible fallback for pyopds2 versions that lack title.
        if title is not None and "title" not in getattr(DataProvider.SearchResponse, "__dataclass_fields__", {}):
            resp.title = title
        # Inject title into pagination params so links carry it.
        # params is a functools.cached_property; setting it on the
        # instance before first access caches our version.
        if title:
            base_params = {
                **({"query": query} if query else {}),
                **({"limit": str(limit)} if limit else {}),
                **({"sort": sort} if sort else {}),
                "title": title,
            }
            if resp.page > 1:
                base_params["page"] = str(resp.page)
            resp.params = base_params
        return resp
