# Link relations

Every link in this feed carries a `rel`, one relation or a list of them,
saying what kind of link it is: `self`, `next`, `search`, or one of OPDS's
own such as `http://opds-spec.org/acquisition/borrow`. The values come from
the [IANA link relations registry](https://www.iana.org/assignments/link-relations/)
or the [OPDS registry](https://specs.opds.io/). Where neither has a word for
what a link is, [RFC 8288](https://www.rfc-editor.org/rfc/rfc8288#section-2.1.2)
lets a feed coin an extension relation: a URL it controls, which resolves to
the page that documents it. This page is that for
`https://openlibrary.org/opds/rel/…`.

The strings are fixed. A relation names a kind of link, so it is the same on
every deployment of the feed, whatever base URL the feed is served from.
A `rel` may be a single string or a list, so check for membership rather than
equality: `has_rel` in pyopds2 does that, as does `hasRel` in the reader.

## Facet groups

A facet group ([OPDS 2.0 §2.4](https://drafts.opds.io/opds-2.0#24-facets))
is a collection with a title and option links; the applied option carries
`self`. The title is for people and may one day be translated; the relation
is for clients. Every option link in a group carries the group's relation,
the applied option as `["self", <relation>]`, so a client finds the
Availability picker by relation whatever its title says. The options' own
identity stays the query parameter and value in their href.

### facet/availability

`https://openlibrary.org/opds/rel/facet/availability`

The group titled Availability. Its options set `mode`: `everything`, the
option with no `mode`, applies no availability filter; `ebooks` keeps books
that can be borrowed or read for free; `open_access` keeps books free to read;
`buyable`, on search pages only, keeps books that can be bought. Several
may be applied as a comma-separated list, and then every listed option
carries `self`.

### facet/language

`https://openlibrary.org/opds/rel/facet/language`

The group titled Language. Its options set `language`, ISO 639-1 codes,
comma-separated with the reader's preferred language first. The option with
no `language` applies no language filter. The links stay single-select: a
selection of several languages marks no option `self`.

### facet/media-type

`https://openlibrary.org/opds/rel/facet/media-type`

The group titled Media Type. Its options set `media_type`: `ebook`,
`audiobook`, or both comma-separated; the option with no `media_type` keeps
both.

### facet/access

`https://openlibrary.org/opds/rel/facet/access`

The group titled Access. Its options set `access`: `general`, the option
with no `access`, hides print-disabled-only books; `print_disabled` shows
only those.

## Carousels

A feed's `groups` are its carousels, each a collection with a `self` link
to the feed it is the first page of. Besides `self`, a carousel's self link
may carry a relation saying what the carousel is. The home feed's Trending
Books carousel and the author page's Popular Books carousel both carry the
registered `http://opds-spec.org/sort/popular`, so a client finds the
popular carousel of any feed by relation. A carousel that needs a relation
of its own will be coined under `https://openlibrary.org/opds/rel/` and
documented here.

## Author pages

An author page, `/authors/<olid>`, is the feed about an author. Its
`metadata.title` is the author's name, `metadata.subtitle` the one-line
summary openlibrary.org shows under their portrait (the English
description of the author's Wikidata item, e.g. "French philosopher,
author, and journalist (1913–1960)") and `metadata.description` their
full biography from the Open Library author record, all three fields of
the Readium Web Publication Manifest metadata that OPDS 2.0's feed
metadata reuses. Either text is absent when its source has none: an
author without a Wikidata description has no subtitle, one without a
biography no description. The feed's `links` carry `alternate` to the
author's page on openlibrary.org, and every book's author contributor
links to the page twice with `rel` `author`: the HTML page as
`text/html`, this feed as `application/opds+json`. The page has the
Popular Books carousel, the author's best-rated books, and lists no
books itself: its one `navigation` entry, titled "All <name>'s books"
and carrying the registered relation `subsection`, leads to
`/authors/<olid>/books`, the paginated feed of all the author's books
with its facets and sorts. The carousel's self link is that feed with
`sort=rating`, and the feed's `up` link (also registered) leads back to
the author page. Filters given to the page (`mode`, `language`,
`media_type`, `access`) narrow the carousel and are carried into both
links.

### portrait

`https://openlibrary.org/opds/rel/portrait`

A picture of the person a feed is about. OPDS 2.0 gives a publication an
`images` collection; its feed schema admits the same collection on a feed,
and this relation on an entry says the image is the person's portrait
rather than a cover or a banner. On an author page it is the author's photo
at `https://covers.openlibrary.org/a/id/<photo>-L.jpg`, with `width` and
`height` the pixel size of that file when it is known.

## Subject schemes

A publication's `metadata.subject` is a list of subject objects
([Readium Web Publication Manifest](https://readium.org/webpub-manifest/contexts/default/#subjects)),
each with a `name` and a link to the feed of books under it. Open Library's
subjects are free text from the catalogue record and carry no `scheme`.
One is not.

### genre

`https://openlibrary.org/opds/rel/genre`

The subject whose `scheme` is this URL is the work's genre: the one of the
catalogue's genres (the front page's navigation) that the most of its
subjects name. It comes first in the list, its `code` is the genre's key
(`science_fiction`), and its link is that genre's feed, the same one the
work's "Popular in" related feed points at. A work in no genre has no such
subject. Not a link relation, but coined here all the same, so that the
scheme a client finds can be opened.

## Link properties

A link's `properties` object is open for extension (Readium Web Publication
Manifest, link schema), and OPDS hangs `price`, `availability`,
`indirectAcquisition` and `authenticate` there. This feed adds one.

### edition

On a work's `rel="alternate"` link of type `application/opds-publication+json`
— one per edition of the work, best offer first — `properties.edition` is a
hint of what the link points at, so a client can list the editions without
fetching any of them. The document behind the link is the truth; the hint
is a summary of it, and may say less.

| Field | Meaning |
|---|---|
| `key` | The Open Library edition, `OL…M` |
| `@type` | `http://schema.org/Book` or `http://schema.org/Audiobook` |
| `author` | The work's author names |
| `published` | The year the printing came out, as an ISO date: `2010-01-01` |
| `language` | ISO 639-1 codes |
| `cover` | An image link: `href`, `type`, `width`, `height` (the `-M` rendition) |
| `distributor` | Who makes the offer, as a reader would read it: `Internet Archive`, `Standard Ebooks`, … |
| `offer` | `rel`, one of `http://opds-spec.org/acquisition/open-access`, `…/borrow`, `…/buy`; with OPDS's own `price` (`{value, currency}`) for a sale and `availability` (`{state}`) for a loan when known |

Every field but `offer` may be absent. A client that does not know the
property fetches the link, as it would any other.
