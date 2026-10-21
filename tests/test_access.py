from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from seminar_bot.access import (
    CACHE_SECONDS,
    MAX_JWKS_BYTES,
    AccessUnavailable,
    AccessVerifier,
)

TEAM = "example-team.cloudflareaccess.com"
AUDIENCE = "ab" * 32


class AccessVerifierTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.next_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_jwk = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(cls.private_key.public_key())
        )
        cls.public_jwk.update({"kid": "first", "alg": "RS256", "use": "sig"})
        cls.next_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.next_key.public_key()))
        cls.next_jwk.update({"kid": "second", "alg": "RS256", "use": "sig"})

    def setUp(self) -> None:
        self.verifier = AccessVerifier(TEAM, AUDIENCE)
        self.fetch = AsyncMock(return_value={"keys": [self.public_jwk]})
        self.verifier._fetch_jwks = self.fetch

    def token(self, *, kid: str = "first", key=None, claims=None, headers=None) -> str:
        now = int(time.time())
        payload = {
            "aud": [AUDIENCE],
            "iss": f"https://{TEAM}",
            "iat": now - 30,
            "exp": now + 300,
            **(claims or {}),
        }
        return jwt.encode(
            payload,
            self.private_key if key is None else key,
            algorithm="RS256",
            headers={"kid": kid, **(headers or {})},
        )

    async def test_valid_assertion_uses_one_cached_fetch(self) -> None:
        assertion = self.token()
        self.assertTrue(await self.verifier.verify(assertion))
        self.assertTrue(await self.verifier.verify(assertion))
        self.fetch.assert_awaited_once()

    async def test_concurrent_first_requests_share_one_fetch(self) -> None:
        async def fetch():
            await asyncio.sleep(0)
            return {"keys": [self.public_jwk]}

        self.fetch.side_effect = fetch
        results = await asyncio.gather(*(self.verifier.verify(self.token()) for _ in range(12)))
        self.assertEqual(results, [True] * 12)
        self.fetch.assert_awaited_once()

    async def test_claims_and_signature_must_match(self) -> None:
        now = int(time.time())
        for claims in (
            {"aud": "cd" * 32},
            {"iss": "https://other-team.cloudflareaccess.com"},
            {"exp": now - 20},
            {"iat": now + 60},
            {"nbf": now + 60},
            {"exp": None},
            {"iat": None},
            {"aud": None},
            {"exp": str(now + 300)},
            {"iat": True},
        ):
            with self.subTest(claims=claims):
                self.assertFalse(await self.verifier.verify(self.token(claims=claims)))
        self.assertFalse(await self.verifier.verify(self.token(key=self.next_key)))

    async def test_critical_claims_are_required(self) -> None:
        now = int(time.time())
        for missing in ("aud", "iss", "iat", "exp"):
            payload = {"aud": AUDIENCE, "iss": f"https://{TEAM}", "iat": now, "exp": now + 300}
            del payload[missing]
            assertion = jwt.encode(
                payload, self.private_key, algorithm="RS256", headers={"kid": "first"}
            )
            with self.subTest(missing=missing):
                self.assertFalse(await self.verifier.verify(assertion))

    async def test_invalid_headers_are_rejected_without_fetching(self) -> None:
        now = int(time.time())
        payload = {"aud": AUDIENCE, "iss": f"https://{TEAM}", "iat": now, "exp": now + 300}
        unsigned = jwt.encode(payload, key="", algorithm="none", headers={"kid": "first"})
        symmetric = jwt.encode(
            payload,
            key="synthetic-test-key-with-enough-length",
            algorithm="HS256",
            headers={"kid": "first"},
        )
        missing_kid = jwt.encode(payload, self.private_key, algorithm="RS256")
        for assertion in (
            "",
            "malformed",
            "x" * 20000,
            unsigned,
            symmetric,
            missing_kid,
            self.token(kid="x" * 201),
        ):
            with self.subTest(length=len(assertion)):
                self.assertFalse(await self.verifier.verify(assertion))
        self.fetch.assert_not_awaited()

    async def test_header_urls_are_never_used_for_key_selection(self) -> None:
        assertion = self.token(
            headers={"jku": "https://attacker.invalid/keys", "x5u": "http://attacker.invalid/cert"}
        )
        self.assertTrue(await self.verifier.verify(assertion))
        self.fetch.assert_awaited_once_with()
        self.assertEqual(self.verifier._certs_url, f"https://{TEAM}/cdn-cgi/access/certs")

    async def test_unknown_kid_refresh_is_rate_bounded_and_supports_rotation(self) -> None:
        clock = 1000.0
        with patch("seminar_bot.access.time.monotonic", side_effect=lambda: clock):
            self.assertTrue(await self.verifier.verify(self.token()))
            self.fetch.return_value = {"keys": [self.public_jwk, self.next_jwk]}
            self.assertFalse(
                await self.verifier.verify(self.token(kid="second", key=self.next_key))
            )
            self.fetch.assert_awaited_once()
            clock += 31
            self.assertTrue(await self.verifier.verify(self.token(kid="second", key=self.next_key)))
            self.assertEqual(self.fetch.await_count, 2)
            for _ in range(10):
                self.assertFalse(await self.verifier.verify(self.token(kid="unknown")))
            self.assertEqual(self.fetch.await_count, 2)

    async def test_expired_cache_does_not_allow_stale_keys_if_refresh_fails(self) -> None:
        clock = 1000.0
        with patch("seminar_bot.access.time.monotonic", side_effect=lambda: clock):
            self.assertTrue(await self.verifier.verify(self.token()))
            self.fetch.side_effect = AccessUnavailable("Keys unavailable")
            clock += CACHE_SECONDS + 1
            with self.assertRaises(AccessUnavailable):
                await self.verifier.verify(self.token())
            with self.assertRaises(AccessUnavailable):
                await self.verifier.verify(self.token())
            self.assertEqual(self.fetch.await_count, 2)

    async def test_malformed_keysets_fail_closed(self) -> None:
        for payload in (
            None,
            {},
            {"keys": []},
            {"keys": [self.public_jwk] * 11},
            {"keys": [self.public_jwk, self.public_jwk]},
            {"keys": [{**self.public_jwk, "kty": "oct"}]},
            {"keys": [{**self.public_jwk, "n": "invalid"}]},
            {"keys": [{**self.public_jwk, "alg": "HS256"}]},
            {"keys": [{**self.public_jwk, "d": "private-material"}]},
        ):
            with self.subTest(payload_type=type(payload).__name__):
                verifier = AccessVerifier(TEAM, AUDIENCE)
                verifier._fetch_jwks = AsyncMock(return_value=payload)
                with self.assertRaises(AccessUnavailable):
                    await verifier.verify(self.token())

    def test_invalid_config_cannot_change_the_key_endpoint(self) -> None:
        for team in (
            "",
            "https://example.cloudflareaccess.com",
            "example.cloudflareaccess.com/keys",
            "example.cloudflareaccess.com.evil.invalid",
            "example@cloudflareaccess.com",
            "EXAMPLE.cloudflareaccess.com",
            "x" * 64 + ".cloudflareaccess.com",
        ):
            with self.subTest(team=team), self.assertRaises(ValueError):
                AccessVerifier(team, AUDIENCE)
        for audience in ("", "not-an-audience", "f" * 63, "z" * 64):
            with self.subTest(audience_length=len(audience)), self.assertRaises(ValueError):
                AccessVerifier(TEAM, audience)


class AccessFetchTests(unittest.IsolatedAsyncioTestCase):
    def session(
        self,
        *,
        status=200,
        chunks=None,
        length=None,
        content_type="application/json",
        encoding=None,
    ):
        response = MagicMock()
        response.status = status
        response.content_type = content_type
        response.content_length = length
        response.headers = {} if encoding is None else {"Content-Encoding": encoding}

        async def stream(_size):
            for chunk in [b'{"keys": []}'] if chunks is None else chunks:
                yield chunk

        response.content.iter_chunked = stream
        request_context = MagicMock()
        request_context.__aenter__ = AsyncMock(return_value=response)
        request_context.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = request_context
        session_context = MagicMock()
        session_context.__aenter__ = AsyncMock(return_value=session)
        session_context.__aexit__ = AsyncMock(return_value=None)
        return session_context, session

    async def test_fetch_only_fixed_https_endpoint_with_timeout_and_no_redirects(self) -> None:
        session_context, session = self.session()
        with patch(
            "seminar_bot.access.aiohttp.ClientSession", return_value=session_context
        ) as factory:
            self.assertEqual(await AccessVerifier(TEAM, AUDIENCE)._fetch_jwks(), {"keys": []})
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["timeout"].total, 5)
        self.assertFalse(kwargs["trust_env"])
        self.assertFalse(kwargs["auto_decompress"])
        session.get.assert_called_once_with(
            f"https://{TEAM}/cdn-cgi/access/certs",
            allow_redirects=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )

    async def test_response_size_is_capped_even_without_content_length(self) -> None:
        for length, chunks in (
            (MAX_JWKS_BYTES + 1, []),
            (None, [b" " * (MAX_JWKS_BYTES // 2), b" " * (MAX_JWKS_BYTES // 2), b"x"]),
        ):
            session_context, _session = self.session(length=length, chunks=chunks)
            with patch("seminar_bot.access.aiohttp.ClientSession", return_value=session_context):
                with self.assertRaises(AccessUnavailable):
                    await AccessVerifier(TEAM, AUDIENCE)._fetch_jwks()

    async def test_redirects_compressed_responses_and_malformed_json_are_rejected(self) -> None:
        for kwargs in (
            {"status": 302},
            {"status": 503},
            {"encoding": "gzip"},
            {"content_type": "text/html"},
            {"chunks": [b"not-json"]},
        ):
            session_context, _session = self.session(**kwargs)
            with patch("seminar_bot.access.aiohttp.ClientSession", return_value=session_context):
                with self.assertRaises(AccessUnavailable):
                    await AccessVerifier(TEAM, AUDIENCE)._fetch_jwks()

    async def test_network_failure_is_unavailable_without_reflecting_details(self) -> None:
        for error in (
            aiohttp.ClientError("private-network-detail"),
            TimeoutError("private-network-detail"),
        ):
            with patch("seminar_bot.access.aiohttp.ClientSession", side_effect=error):
                with self.assertRaises(AccessUnavailable) as raised:
                    await AccessVerifier(TEAM, AUDIENCE)._fetch_jwks()
                self.assertNotIn("private-network-detail", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
