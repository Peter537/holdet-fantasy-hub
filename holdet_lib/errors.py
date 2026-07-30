"""Expected, user-facing scraper errors."""


class ScraperError(Exception):
    """Base class for expected scraper failures."""


class UrlValidationError(ScraperError):
    """Raised when an input URL is not supported."""


class FetchError(ScraperError):
    """Raised when a public resource cannot be downloaded."""


class PayloadError(ScraperError):
    """Raised when a server-rendered or JSON payload is incompatible."""


class UnsupportedGameError(ScraperError):
    """Raised when a Holdet.dk game variant is intentionally unsupported."""
