from __future__ import annotations

import json
import logging
from typing import Any

from jev_trader.execution.bitfinex.auth import Nonce, rest_headers
from jev_trader.execution.bitfinex.errors import BitfinexAPIError, scrub

logger = logging.getLogger(__name__)


class BitfinexREST:
    """Authenticated and public REST. Request headers are never logged."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        rest_url: str = "https://api.bitfinex.com",
        public_rest_url: str = "https://api-pub.bitfinex.com",
        transport: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._rest_url = rest_url.rstrip("/")
        self._public_rest_url = public_rest_url.rstrip("/")
        self._nonce = Nonce()
        self._transport = transport
        self._client: Any | None = None

    async def __aenter__(self) -> BitfinexREST:
        if self._transport is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get_public(self, path: str) -> Any:
        url = self._public_rest_url + path
        if self._transport is not None:
            return await self._transport("GET", path, None)
        return await self._request("GET", url, path=path, headers=None, body=None)

    async def post_auth(self, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = body if body is not None else {}
        raw = json.dumps(payload, separators=(",", ":"))
        nonce = self._nonce.next()
        headers = rest_headers(self._api_key, self._api_secret, path, nonce, raw)
        if self._transport is not None:
            return await self._transport("POST", path, payload)
        url = self._rest_url + path
        return await self._request("POST", url, path=path, headers=headers, body=raw)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        path: str,
        headers: dict[str, str] | None,
        body: str | None,
    ) -> Any:
        if self._client is None:
            raise BitfinexAPIError("REST client is not open")
        try:
            response = await self._client.request(method, url, headers=headers, content=body)
        except Exception as exc:
            raise BitfinexAPIError(f"{method} {path} failed") from exc
        text = response.text
        if response.status_code >= 400:
            safe = scrub(text, (self._api_key, self._api_secret))
            logger.warning("bitfinex %s %s -> %s %s", method, path, response.status_code, safe)
            raise BitfinexAPIError(
                f"{method} {path} -> {response.status_code}",
                status=response.status_code,
            )
        try:
            return response.json()
        except Exception as exc:
            raise BitfinexAPIError(f"{method} {path} returned non-json") from exc
