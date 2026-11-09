from __future__ import annotations

import hashlib
import hmac
import time


class Nonce:
    """Strictly increasing microsecond nonce. Bitfinex rejects a reused nonce."""

    def __init__(self) -> None:
        self._last = 0

    def next(self) -> str:
        now = time.time_ns() // 1_000
        if now <= self._last:
            now = self._last + 1
        self._last = now
        return str(now)


def sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha384)
    return digest.hexdigest()


def rest_sig_payload(path: str, nonce: str, body: str) -> str:
    """Canonical string signed for REST. `path` is the `/v2/...` path, without `/api`."""
    return f"/api{path}{nonce}{body}"


def rest_headers(api_key: str, api_secret: str, path: str, nonce: str, body: str) -> dict[str, str]:
    return {
        "bfx-apikey": api_key,
        "bfx-nonce": nonce,
        "bfx-signature": sign(api_secret, rest_sig_payload(path, nonce, body)),
        "content-type": "application/json",
    }


def ws_auth_message(api_key: str, api_secret: str, nonce: str) -> dict[str, str]:
    payload = f"AUTH{nonce}"
    return {
        "event": "auth",
        "apiKey": api_key,
        "authNonce": nonce,
        "authPayload": payload,
        "authSig": sign(api_secret, payload),
    }
