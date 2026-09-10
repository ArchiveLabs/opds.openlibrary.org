# WAF Endpoint Patterns

Regex patterns for all valid routes, intended for WAF allow-list configuration.

**Path frame.** These patterns are relative to the *application* root — the path
as the app sees it, after the fronting nginx has stripped the `/opds` prefix.
The public address is `https://openlibrary.org/opds/…`, so a WAF that matches
the raw request path at the edge must prefix every pattern below with `/opds`
(e.g. `^/opds/search(\?.*)?$`); applied unprefixed at the edge, the allow-list
matches nothing.

## Individual Routes

| Route | Method | Regex |
|---|---|---|
| Homepage | GET | `^/(\?.*)?$` |
| Search | GET | `^/search(\?.*)?$` |
| Single Edition | GET | `^/books/OL[0-9]+M$` |
| Author Catalog | GET | `^/authors/OL[0-9]+A(\?.*)?$` |
| Health Check | GET | `^/health$` |
| Service Worker | GET | `^/sw\.js$` |

## Combined Allow-List

```
^/(\?.*|search(\?.*)?|books/OL[0-9]+M|authors/OL[0-9]+A(\?.*)?|health|sw\.js)?$
```

## Notes

- **Allowed methods**: GET, HEAD, OPTIONS only
- **Homepage query params**: `/` takes `mode`, `language`, `page`, `media_type`, `access` and `limit`, and the feed itself links to `/?language=…`, `/?access=…` and `/?page=…` — a homepage pattern without `(\?.*)?` blocks the service's own facet and pagination links
- **Search query params**: `/search` query parameters contain Solr syntax (`[]`, `*`, `:`, spaces) — the WAF must not block these as injection attempts
- **Docs endpoints**: `/docs`, `/redoc`, `/openapi.json` are disabled unconditionally in the `FastAPI(...)` constructor; block them at the WAF as well
- **Sentry debug**: `/sentry-debug` is always registered but answers 404 when `ENVIRONMENT == "production"` — block in production WAF rules
