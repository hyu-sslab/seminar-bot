from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import asdict, replace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote, urlsplit

import aiohttp

from seminar_bot import paper_metadata
from seminar_bot.documents import DocumentInfo
from seminar_bot.paper_metadata import PaperMetadataResolver

DOI = "10.14722/ndss.2025.230822"
TITLE = (
    "Incorporating Gradients to Rules: Towards Lightweight, Adaptive "
    "Provenance-based Intrusion Detection"
)
TEXT = (
    "Proceedings of a security symposium\nIncorporating Gradients to Rules: Towards\n"
    "Lightweight, Adaptive Provenance-based Intrusion Detection\n"
    "Example Authors\nAbstract—This paper describes a detection system.\n"
)


def document(**changes):
    info = DocumentInfo(
        "Incorporating Gradients to Rules: Towards",
        TEXT,
        DOI,
        None,
        "a" * 64,
        "pdf_metadata",
        "original warning",
    )
    return replace(info, **changes)


def crossref(title=TITLE, doi=DOI):
    return {"status": "ok", "message": {"DOI": doi, "title": [title]}}


def datacite(title=TITLE, doi=DOI):
    return {
        "data": {
            "id": doi,
            "type": "dois",
            "attributes": {"doi": doi, "titles": [{"title": title}]},
        }
    }


class FakeContent:
    def __init__(self, chunks, gate=None):
        self.chunks, self.gate, self.read = chunks, gate, 0

    async def iter_chunked(self, size):
        if self.gate is not None:
            await self.gate.wait()
        for chunk in self.chunks:
            self.read += 1
            yield chunk


class FakeResponse:
    def __init__(
        self, payload=None, *, status=200, chunks=None, headers=None, length=None, gate=None
    ):
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.content_length = length
        self.content = FakeContent(
            [json.dumps(payload).encode()] if chunks is None else chunks, gate
        )
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class PaperMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.responses = []
        self.calls = []
        self.sessions = []
        self.resolver = PaperMetadataResolver()
        outer = self

        class FakeSession:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.closed = False
                outer.sessions.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                self.closed = True

            def get(self, url, **kwargs):
                outer.calls.append((url, kwargs))
                response = outer.responses.pop(0) if outer.responses else FakeResponse(status=404)
                if isinstance(response, Exception):
                    raise response
                return response

        mocked = patch.object(paper_metadata.aiohttp, "ClientSession", FakeSession)
        mocked.start()
        self.addCleanup(mocked.stop)

    async def test_ndss_multiline_title_replaces_incorrect_pdf_metadata_and_nothing_else(self):
        original = document(title="Incorrect publisher metadata", doi=DOI.upper())
        self.responses.append(
            FakeResponse(
                crossref(), headers={"Content-Type": "application/vnd.crossref-api-message+json"}
            )
        )
        enriched = await self.resolver.enrich(original)
        self.assertEqual(enriched.title, TITLE)
        self.assertEqual(asdict(enriched), {**asdict(original), "title": TITLE})
        self.assertEqual(original.title, "Incorrect publisher metadata")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.sessions[0].closed)

    async def test_markup_entities_and_wrapped_hyphens_become_plain_official_title(self):
        title = "Privacy & Security for Adaptive Intrusion Detection"
        self.responses.append(
            FakeResponse(
                crossref(
                    "<i>Privacy</i> &amp; Security for <b>Adaptive Intrusion Detection</b><script>private</script>"
                )
            )
        )
        enriched = await self.resolver.enrich(document(text=title + "\nAuthors\nAbstract: Body"))
        self.assertEqual(enriched.title, title)
        self.responses.append(FakeResponse(crossref()))
        wrapped = TEXT.replace("Lightweight", "Light-\nweight")
        self.assertEqual(
            (await PaperMetadataResolver().enrich(document(text=wrapped))).title, TITLE
        )

    async def test_crossref_missing_falls_back_to_datacite_with_exact_doi(self):
        self.responses.extend([FakeResponse(status=404), FakeResponse(datacite())])
        enriched = await self.resolver.enrich(document())
        self.assertEqual(enriched.title, TITLE)
        self.assertEqual(
            [urlsplit(url).netloc for url, _ in self.calls],
            ["api.crossref.org", "api.datacite.org"],
        )
        self.assertEqual(
            [unquote(urlsplit(url).path.split("/")[-1]) for url, _ in self.calls], [DOI, DOI]
        )

    async def test_doi_mismatch_from_either_provider_is_never_accepted(self):
        self.responses.extend(
            [
                FakeResponse(crossref(doi="10.9999/other")),
                FakeResponse(datacite(doi="10.9999/other")),
            ]
        )
        original = document()
        self.assertIs(await self.resolver.enrich(original), original)
        self.assertEqual(self.resolver._cache, {})

    async def test_datacite_ignores_translations_and_malformed_title_types(self):
        payload = datacite()
        payload["data"]["attributes"]["titles"] = [
            {"title": "A Translated Title", "titleType": "TranslatedTitle"},
            {"title": "Malformed metadata", "titleType": []},
            {"title": TITLE, "titleType": None},
        ]
        self.responses.extend([FakeResponse(status=404), FakeResponse(payload)])
        self.assertEqual((await self.resolver.enrich(document())).title, TITLE)

    async def test_cached_title_is_checked_against_each_document_not_reused_as_a_match(self):
        self.responses.append(FakeResponse(crossref()))
        cited = document(text="A Different Research Paper\nAuthors\n1 Introduction\n" + TITLE)
        self.assertIs(await self.resolver.enrich(cited), cited)
        self.assertEqual((await self.resolver.enrich(document())).title, TITLE)
        self.assertIs(await self.resolver.enrich(cited), cited)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.resolver._cache.values())[0][1], TITLE)

    async def test_citations_inline_references_prose_and_late_titles_do_not_overwrite(self):
        self.responses.append(FakeResponse(crossref()))
        for text in [
            "A Different Research Paper\nReferences [1] Some author\n" + TITLE,
            "A Different Research Paper\nAbstract: We discuss work\n" + TITLE,
            "We compare the paper " + TITLE + " with earlier studies.",
            "Preface\n" + "x" * 3100 + "\n" + TITLE,
        ]:
            with self.subTest(text=text[:40]):
                original = document(text=text)
                self.assertIs(await self.resolver.enrich(original), original)

    async def test_short_one_or_two_word_titles_require_conservative_evidence(self):
        for title in ["Learning", "Deep Learning", "Electroencephalographically"]:
            with self.subTest(title=title):
                self.responses.append(FakeResponse(crossref(title)))
                original = document(text=title + "\nAuthors\nAbstract: Body")
                self.assertIs(await PaperMetadataResolver().enrich(original), original)
        title = "Hyperdimensional Computing"
        self.responses.append(FakeResponse(crossref(title)))
        self.assertEqual(
            (
                await PaperMetadataResolver().enrich(document(text=title + "\nAuthors\nAbstract"))
            ).title,
            title,
        )

    async def test_missing_text_and_invalid_doi_do_not_issue_network_requests(self):
        cases = [document(text=""), document(doi=None)]
        cases.extend(
            document(doi=doi)
            for doi in [
                "https://private.invalid/",
                "//127.0.0.1/",
                "10.1234/x\nCookie:y",
                "10.1234/x\ud800",
                "10.1234/" + "a" * 256,
            ]
        )
        for original in cases:
            self.assertIs(await self.resolver.enrich(original), original)
        self.assertEqual(self.calls, [])

    async def test_only_encoded_doi_and_fixed_anonymous_headers_leave_the_process(self):
        doi = "10.1234/a?next=https://127.0.0.1/#private"
        original = document(doi=doi)
        self.responses.append(FakeResponse(crossref(doi=doi)))
        await self.resolver.enrich(original)
        url, options = self.calls[0]
        self.assertEqual(urlsplit(url).scheme, "https")
        self.assertEqual(urlsplit(url).netloc, "api.crossref.org")
        self.assertEqual(urlsplit(url).query, "")
        self.assertEqual(urlsplit(url).fragment, "")
        self.assertEqual(unquote(urlsplit(url).path.split("/")[-1]), doi)
        self.assertFalse(options["allow_redirects"])
        self.assertEqual(options["timeout"].total, 3)
        session = self.sessions[0]
        self.assertFalse(session.options["trust_env"])
        self.assertFalse(session.options["auto_decompress"])
        self.assertIsInstance(session.options["cookie_jar"], aiohttp.DummyCookieJar)
        headers = session.options["headers"]
        self.assertEqual(set(headers), {"Accept", "Accept-Encoding", "User-Agent"})
        self.assertEqual(headers["Accept-Encoding"], "identity")
        self.assertFalse({"data", "json", "cookies", "auth"} & options.keys())
        self.assertNotIn(original.text, json.dumps((url, headers)))
        self.assertNotIn(original.sha256, json.dumps((url, headers)))

    async def test_redirects_and_non_json_or_compressed_responses_are_not_followed(self):
        for first in [
            FakeResponse(status=302, headers={"Location": "https://127.0.0.1/private"}),
            FakeResponse(crossref(), headers={"Content-Type": "text/html"}),
            FakeResponse(crossref(), headers={"Content-Encoding": "gzip"}),
        ]:
            self.responses.extend([first, FakeResponse(status=404)])
            original = document()
            self.assertIs(await PaperMetadataResolver().enrich(original), original)
            self.assertTrue(first.closed)
        self.assertEqual(
            {urlsplit(url).netloc for url, _ in self.calls},
            {"api.crossref.org", "api.datacite.org"},
        )

    async def test_declared_and_streamed_metadata_have_a_half_mib_limit(self):
        maximum = paper_metadata.MAX_METADATA_BYTES
        declared = FakeResponse(crossref(), length=maximum + 1)
        streamed = FakeResponse(
            chunks=[b"x" * (maximum // 2), b"y" * (maximum // 2), b"z", b"must-not-read"]
        )
        for response in (declared, streamed):
            self.responses.extend([response, FakeResponse(status=404)])
            original = document()
            self.assertIs(await PaperMetadataResolver().enrich(original), original)
            self.assertTrue(response.closed)
        self.assertEqual(declared.content.read, 0)
        self.assertEqual(streamed.content.read, 3)

    async def test_invalid_json_shapes_utf8_and_overlong_titles_preserve_original(self):
        for response in [
            FakeResponse(chunks=[b"{bad"]),
            FakeResponse(chunks=[b"\xff"]),
            FakeResponse([]),
            FakeResponse(crossref("x" * 513)),
            FakeResponse({"status": "ok", "message": {"DOI": DOI, "title": "not-a-list"}}),
        ]:
            self.responses.extend([response, FakeResponse(status=404)])
            original = document()
            self.assertIs(await PaperMetadataResolver().enrich(original), original)

    async def test_each_provider_has_its_own_timeout_and_total_lookup_is_bounded(self):
        stalled = FakeResponse(crossref(), gate=asyncio.Event())
        self.responses.extend([stalled, FakeResponse(datacite())])
        with patch.object(paper_metadata, "PROVIDER_TIMEOUT_SECONDS", 0.01):
            self.assertEqual((await self.resolver.enrich(document())).title, TITLE)
        self.assertTrue(stalled.closed)
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.responses.append(FakeResponse(crossref(), gate=asyncio.Event()))
        original = document()
        with patch.object(paper_metadata, "LOOKUP_TIMEOUT_SECONDS", 0.01):
            self.assertIs(await PaperMetadataResolver().enrich(original), original)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.sessions[-1].closed)

    async def test_blocked_network_preserves_original_and_cancellation_closes_session(self):
        self.responses.extend(
            [
                OSError("synthetic blocked network"),
                aiohttp.ClientConnectionError("synthetic blocked network"),
            ]
        )
        original = document()
        self.assertIs(await self.resolver.enrich(original), original)
        stalled = FakeResponse(crossref(), gate=asyncio.Event())
        self.responses.append(stalled)
        pending = asyncio.create_task(self.resolver.enrich(original))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertTrue(stalled.closed)
        self.assertTrue(self.sessions[-1].closed)
        self.assertEqual(self.resolver._cache, {})

    async def test_cache_expires_after_six_hours_and_never_exceeds_256_titles(self):
        lookup = AsyncMock(return_value=TITLE)
        with (
            patch.object(self.resolver, "_lookup", lookup),
            patch.object(paper_metadata, "monotonic", return_value=100),
        ):
            await self.resolver.enrich(document())
            await self.resolver.enrich(document())
            self.assertEqual(lookup.await_count, 1)
        with (
            patch.object(self.resolver, "_lookup", lookup),
            patch.object(paper_metadata, "monotonic", return_value=100 + 6 * 60 * 60),
        ):
            await self.resolver.enrich(document())
            self.assertEqual(lookup.await_count, 2)
            for number in range(256):
                await self.resolver.enrich(document(doi=f"10.1234/cache-{number}"))
        self.assertEqual(len(self.resolver._cache), 256)
        self.assertNotIn(DOI, self.resolver._cache)
        for expires, title in self.resolver._cache.values():
            self.assertIsInstance(expires, (int, float))
            self.assertEqual(title, TITLE)
