from __future__ import annotations

__all__ = [
    "OPDSException",
    "EditionNotFound",
    "AuthorNotFound",
    "UpstreamError",
]


class OPDSException(Exception):
    pass


class EditionNotFound(OPDSException):
    def __init__(self, edition_olid: str):
        self.edition_olid = edition_olid
        super().__init__(f"Edition not found: {edition_olid}")


class AuthorNotFound(OPDSException):
    def __init__(self, author_olid: str):
        self.author_olid = author_olid
        super().__init__(f"Author not found: {author_olid}")


class UpstreamError(OPDSException):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)
