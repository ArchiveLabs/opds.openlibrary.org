# WAF Endpoint Patterns

Regex patterns for all valid routes, intended for WAF allow-list configuration.

## Individual Routes

| Route | Method | Regex |
|---|---|---|
| Homepage | GET | `^/$` |
| Search | GET | `^/search(\?.*)?$` |
| Single Edition | GET | `^/books/OL[0-9]+M$` |
| Author Catalog | GET | `^/authors/OL[0-9]+A(\?.*)?$` |
| Health Check | GET | `^/health$` |
| Service Worker | GET | `^/sw\.js$` |

## Combined Allow-List

```
^/(search(\?.*)?|books/OL[0-9]+M|authors/OL[0-9]+A(\?.*)?|health|sw\.js)?$
```

## Notes

- **Allowed methods**: GET, HEAD, OPTIONS only
- **Search query params**: `/search` query parameters contain Solr syntax (`[]`, `*`, `:`, spaces) — the WAF must not block these as injection attempts
- **Docs endpoints**: `/docs`, `/redoc`, `/openapi.json` are disabled in production via the `ENVIRONMENT=production` setting and should be blocked by WAF as well
- **Sentry debug**: `/sentry-debug` only exists when `ENVIRONMENT != "production"` — block in production WAF rules
