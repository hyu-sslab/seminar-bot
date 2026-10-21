"""Validate Access assertions at the origin using a bounded, fixed JWKS endpoint."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from typing import Any

import aiohttp
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

MAX_JWKS_BYTES = 128 * 1024
MAX_JWKS_KEYS = 10
CACHE_SECONDS = 300
REFRESH_INTERVAL_SECONDS = 30
MAX_ASSERTION_BYTES = 16 * 1024


class AccessUnavailable(RuntimeError):
    """Access verification cannot obtain a valid current signing key set."""


class AccessVerifier:
    def __init__(self, team_domain: str, audience: str) -> None:
        if (
            not isinstance(team_domain, str)
            or not re.fullmatch(r"[a-z0-9-]+\.cloudflareaccess\.com", team_domain)
            or len(team_domain.split(".", 1)[0]) > 63
        ):
            raise ValueError("Invalid Cloudflare Access team domain")
        if not isinstance(audience, str) or not re.fullmatch(r"[a-f0-9]{64}", audience):
            raise ValueError("Invalid Cloudflare Access application audience")
        self._issuer = f"https://{team_domain}"
        self._certs_url = f"{self._issuer}/cdn-cgi/access/certs"
        self._audience = audience
        self._keys: dict[str, RSAPublicKey] = {}
        self._expires_at = 0.0
        self._last_attempt = float("-inf")
        self._last_error = False
        self._lock = asyncio.Lock()

    async def verify(self, assertion: str) -> bool:
        if (
            not isinstance(assertion, str)
            or not assertion
            or len(assertion) > MAX_ASSERTION_BYTES
            or not assertion.isascii()
        ):
            return False
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.InvalidTokenError:
            return False
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not 1 <= len(kid) <= 200
            or header.get("crit")
            or header.get("b64") is False
        ):
            return False
        # No header-provided URL or key is used. Only this configured team's
        # fixed HTTPS endpoint can supply a key matching the header's kid.
        key = await self._key(kid)
        if key is None:
            return False
        try:
            claims = jwt.decode(
                assertion,
                key=key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                leeway=5,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            for field in ("exp", "iat"):
                value = claims[field]
                if type(value) not in (int, float) or not math.isfinite(value):
                    return False
            return claims["exp"] > claims["iat"]
        except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
            return False

    async def _key(self, kid: str) -> RSAPublicKey | None:
        now = time.monotonic()
        if now < self._expires_at and kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            now = time.monotonic()
            if now < self._expires_at and kid in self._keys:
                return self._keys[kid]
            # Unknown kids cannot trigger one outbound request per assertion.
            # Failed refreshes share the same backoff, including a cold cache.
            if now - self._last_attempt < REFRESH_INTERVAL_SECONDS:
                if self._last_error:
                    raise AccessUnavailable("Access signing keys are temporarily unavailable")
                return None
            self._last_attempt = now
            try:
                keys = self._parse_keys(await self._fetch_jwks())
            except AccessUnavailable:
                self._last_error = True
                raise
            self._keys = keys
            self._expires_at = time.monotonic() + CACHE_SECONDS
            self._last_error = False
            return self._keys.get(kid)

    async def _fetch_jwks(self) -> Any:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5),
                trust_env=False,
                auto_decompress=False,
            ) as session:
                async with session.get(
                    self._certs_url,
                    allow_redirects=False,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if (
                        response.status != 200
                        or response.content_type != "application/json"
                        or response.headers.get("Content-Encoding", "identity").lower()
                        != "identity"
                        or (
                            response.content_length is not None
                            and response.content_length > MAX_JWKS_BYTES
                        )
                    ):
                        raise AccessUnavailable("Access signing keys are temporarily unavailable")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_JWKS_BYTES:
                            raise AccessUnavailable(
                                "Access signing keys are temporarily unavailable"
                            )
                    return json.loads(body)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError, RecursionError):
            raise AccessUnavailable("Access signing keys are temporarily unavailable") from None

    @staticmethod
    def _parse_keys(payload: Any) -> dict[str, RSAPublicKey]:
        if not isinstance(payload, dict):
            raise AccessUnavailable("Access signing key set is invalid")
        entries = payload.get("keys")
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_JWKS_KEYS:
            raise AccessUnavailable("Access signing key set is invalid")
        keys: dict[str, RSAPublicKey] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise AccessUnavailable("Access signing key set is invalid")
            kid = entry.get("kid")
            if (
                not isinstance(kid, str)
                or not 1 <= len(kid) <= 200
                or kid in keys
                or entry.get("kty") != "RSA"
                or entry.get("alg", "RS256") != "RS256"
                or entry.get("use", "sig") != "sig"
                or any(field in entry for field in ("d", "p", "q", "dp", "dq", "qi", "oth", "k"))
                or not isinstance(entry.get("n"), str)
                or not 1 <= len(entry["n"]) <= 1400
                or not isinstance(entry.get("e"), str)
                or not 1 <= len(entry["e"]) <= 16
            ):
                raise AccessUnavailable("Access signing key set is invalid")
            try:
                key = jwt.algorithms.RSAAlgorithm.from_jwk(entry)
                if not isinstance(key, RSAPublicKey) or not 2048 <= key.key_size <= 8192:
                    raise ValueError("Invalid RSA signing key")
            except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
                raise AccessUnavailable("Access signing key set is invalid") from None
            keys[kid] = key
        return keys
