from __future__ import annotations


class BitfinexError(Exception):
    """Base error for the Bitfinex adapter. Never carries credentials."""


class BitfinexAPIError(BitfinexError):
    """A transport, auth, or protocol failure. Already counted toward the kill switch."""

    counted = True

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class ForeignExposure(BitfinexError):
    """The sub-account holds positions or orders this process does not own."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Remove credential material from a message before it is logged."""
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:300]
