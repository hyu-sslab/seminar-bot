from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, date, datetime
from unittest.mock import patch

import test_web_api as fixtures

from seminar_bot.documents import DocumentInfo
from seminar_bot.web_api import document_version, session_version

NOW = datetime(2026, 10, 27, 3, tzinfo=UTC)


class ArchiveWebAPITests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.WebAPITests.asyncTearDown
    headers = fixtures.WebAPITests.headers
    assert_private = fixtures.WebAPITests.assert_private

    async def asyncSetUp(self):
        with patch.object(fixtures, "datetime") as fixture_clock:
            fixture_clock.now.return_value = NOW
            await fixtures.WebAPITests.asyncSetUp(self)
        self.clock = patch("seminar_bot.web_api.local_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with self.bot.store.db:
            self.bot.store.db.execute(
                "UPDATE sessions SET seminar_date='2026-09-03' WHERE id=?", (self.row["id"],)
            )
        self.row = self.bot.store.session(1, self.row["id"])
        self.version = session_version(self.row)
        self.bot.store.add_document(
            1,
            8001,
            101,
            DocumentInfo("Operational Presentation", "", None, None, "a" * 64, "pdf_text"),
            "operational.pdf",
            "",
            "",
            NOW,
            kind="slides",
            session_id=self.row["id"],
            source_type="web",
            upload_key="00000000-0000-4000-8000-000000000001",
        )
        self.bot.settings.rotation_file.write_text(
            json.dumps({"presenters": [{"user_id": "101", "name": "Current Display Name"}]}),
            encoding="utf-8",
        )
        self.slide_bytes = fixtures.pdf_bytes("Archived slides", "Stored presentation content")
        self.imported = self.bot.store.insert_archive_batch(
            1,
            [
                self.group("year", "Historical Presenter", None),
                self.group("dated", "Dated Presenter", date(2026, 7, 21)),
                self.group("unassigned", "Unassigned Presenter", None, unassigned=True),
            ],
            imported_at=NOW,
        )
        self.groups = {
            row["import_key"].rsplit("/", 1)[-1]: row["id"] for row in self.imported["groups"]
        }
        self.documents = {
            row["import_key"].removeprefix("private-import/"): self.bot.store.document(1, row["id"])
            for row in self.imported["documents"]
        }

    def group(self, key, name, day, *, unassigned=False):
        materials = []
        if not unassigned:
            relative = f"files/history/{key}-slides.pdf"
            path = self.data_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.slide_bytes)
            materials.append(
                {
                    "import_key": f"private-import/{key}/slides",
                    "kind": "slides",
                    "filename": f"{key}-slides.pdf",
                    "file_path": relative,
                    "info": DocumentInfo(
                        f"Archived {key} presentation",
                        "PRIVATE_ARCHIVE_EXTRACTED_BODY",
                        None,
                        None,
                        hashlib.sha256(self.slide_bytes).hexdigest(),
                        "pdf_text",
                    ),
                }
            )
        materials.append(
            {
                "import_key": f"private-import/{key}/paper",
                "kind": "paper",
                "filename": f"{key}-paper.pdf",
                "info": DocumentInfo(
                    f"Archived {key} research paper",
                    "PRIVATE_PAPER_TEXT_MUST_NOT_SURVIVE",
                    "10.1234/archived-paper",
                    None,
                    hashlib.sha256(key.encode()).hexdigest(),
                    "pdf_text",
                ),
                "venue": "Research Symposium",
                "year": 2024,
                "link": "https://example.test/research",
            }
        )
        return {
            "import_key": f"private-group/{key}",
            "presenter_id": 101,
            "presenter_name_snapshot": name,
            "seminar_year": 2026,
            "seminar_date": day,
            "label": f"Reviewed {key} topic",
            "archive_kind": "unassigned" if unassigned else "presentation",
            "documents": materials,
        }

    def operational_state(self):
        return {
            table: [tuple(row) for row in self.bot.store.db.execute(f"SELECT * FROM {table}")]
            for table in (
                "guilds",
                "sessions",
                "holidays",
                "deliveries",
                "rotation_file_state",
                "submission_invitations",
                "submission_grants",
            )
        }

    def assert_safe_dto(self, payload, *tokens):
        raw = json.dumps(payload, ensure_ascii=False)
        for private in (
            "file_path",
            "import_key",
            "input_fingerprint",
            "extracted_text",
            "search_text",
            "PRIVATE_ARCHIVE_EXTRACTED_BODY",
            "PRIVATE_PAPER_TEXT_MUST_NOT_SURVIVE",
            "private-import/",
            "private-group/",
            str(self.data_dir),
            self.bot.settings.api_token,
            *tokens,
        ):
            self.assertNotIn(private, raw)

    def grant(self):
        invitation, _ = self.api.submission_access.issue(1, self.row["id"], 101, scope="presenter")
        token, _ = self.api.submission_access.redeem(invitation, 1)
        return token

    async def test_presentations_keep_colliding_ids_year_only_dates_and_snapshot_names(self):
        self.assertEqual(self.groups["year"], self.row["id"])
        before = self.operational_state()
        response = await self.client.get("/v1/presentations", headers=self.headers())
        self.assertEqual(response.status, 200, await response.text())
        self.assert_private(response)
        data = await response.json()
        self.assertEqual(data["total"], 4)
        self.assertEqual(
            [item["session"]["record_key"] for item in data["presentations"]],
            [
                f"session:{self.row['id']}",
                f"archive:{self.groups['dated']}",
                f"archive:{self.groups['unassigned']}",
                f"archive:{self.groups['year']}",
            ],
        )
        by_key = {item["session"]["record_key"]: item for item in data["presentations"]}
        unknown = by_key[f"archive:{self.groups['year']}"]
        session = unknown["session"]
        self.assertIsNone(session["seminar_date"])
        self.assertEqual(session["seminar_year"], 2026)
        self.assertEqual(session["date_precision"], "year")
        self.assertEqual(session["presenter_name"], "Historical Presenter")
        self.assertTrue(session["archived"])
        self.assertNotIn("version", session)
        self.assertEqual((unknown["slides_count"], unknown["paper_count"]), (1, 1))
        self.assertTrue(all(not item["editable"] for item in unknown["documents"]))
        dated = by_key[f"archive:{self.groups['dated']}"]["session"]
        self.assertEqual(dated["seminar_date"], "2026-07-21")
        self.assertEqual(dated["date_precision"], "day")
        self.assertEqual(
            by_key[f"archive:{self.groups['unassigned']}"]["session"]["archive_kind"],
            "unassigned",
        )
        self.assert_safe_dto(data)
        self.assertEqual(self.operational_state(), before)

    async def test_paper_archive_exposes_seminar_year_separately_from_publication_year(self):
        response = await self.client.get("/v1/archive?kind=paper", headers=self.headers())
        self.assertEqual(response.status, 200, await response.text())
        self.assert_private(response)
        data = await response.json()
        self.assertEqual(data["total"], 3)
        documents = {item["record_key"]: item for item in data["documents"]}
        paper = documents[f"archive:{self.groups['year']}"]
        self.assertEqual(paper["seminar_year"], 2026)
        self.assertEqual(paper["year"], 2024)
        self.assertIsNone(paper["seminar_date"])
        self.assertIsNone(paper["session_id"])
        self.assertEqual(paper["presenter_name"], "Historical Presenter")
        self.assertEqual(paper["date_precision"], "year")
        self.assertFalse(paper["file_available"])
        self.assertFalse(paper["editable"])
        self.assertEqual(paper["source"], "import")
        self.assert_safe_dto(data)
        record = self.bot.store.document(1, paper["id"])
        self.assertEqual(record["extracted_text"], "")
        self.assertEqual(record["file_path"], "")

    async def test_history_search_uses_snapshot_names_years_topics_and_paper_titles(self):
        for query, expected in (
            ("Historical Presenter", [self.groups["year"]]),
            ("Reviewed dated topic", [self.groups["dated"]]),
            ("2026", list(self.groups.values())),
            ("Archived unassigned research", [self.groups["unassigned"]]),
        ):
            response = await self.client.get(
                "/v1/presentations", params={"q": query}, headers=self.headers()
            )
            self.assertEqual(response.status, 200, await response.text())
            data = await response.json()
            self.assertEqual(data["total"], len(expected))
            self.assertEqual(
                {item["session"]["record_key"] for item in data["presentations"]},
                {f"archive:{value}" for value in expected},
            )
        response = await self.client.get(
            "/v1/archive",
            params={"kind": "paper", "q": "Archived year research"},
            headers=self.headers(),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["total"], 1)

    async def test_anonymous_and_submission_links_cannot_browse_or_download_history(self):
        grant = self.grant()
        before = self.operational_state()
        file = f"/v1/documents/{self.documents['year/slides']['id']}/file"
        for endpoint in ("/v1/presentations", "/v1/archive?kind=paper", file):
            response = await self.client.get(endpoint)
            self.assertEqual(response.status, 401)
            self.assert_private(response)
            denied = await self.client.get(
                endpoint, headers=self.headers(**{"X-Seminar-Submission-Token": grant})
            )
            self.assertEqual(denied.status, 403)
            self.assert_safe_dto(await denied.json(), grant)
        self.assertEqual(self.operational_state(), before)

    async def test_private_context_and_colliding_session_ids_never_edit_archived_documents(self):
        grant = self.grant()
        headers = self.headers(**{"X-Seminar-Submission-Token": grant})
        context = await self.client.get("/v1/submission/context", headers=headers)
        self.assertEqual(context.status, 200)
        context_data = await context.json()
        self.assertTrue(all(item["seminar_date"] for item in context_data["sessions"]))
        self.assertTrue(all("archive_kind" not in item for item in context_data["sessions"]))
        response = await self.client.get(f"/v1/sessions/{self.groups['year']}", headers=headers)
        self.assertEqual(response.status, 200)
        live = (await response.json())["session"]
        self.assertEqual(live["record_type"], "session")
        self.assertEqual(live["record_key"], f"session:{self.row['id']}")
        history = self.documents["year/paper"]
        before = "\n".join(self.bot.store.db.iterdump())
        for method in ("patch", "delete"):
            denied = await getattr(self.client, method)(
                f"/v1/sessions/{self.row['id']}/documents/{history['id']}",
                headers={**headers, "X-Document-Version": document_version(history)},
                json={"title": "Forbidden rewrite"} if method == "patch" else {},
            )
            self.assertEqual(denied.status, 404, await denied.text())
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
        self.bot.notify_web_submission.assert_not_awaited()

    async def test_archived_slides_download_requires_auth_and_preserves_original_bytes(self):
        slide = self.documents["year/slides"]
        response = await self.client.get(
            f"/v1/documents/{slide['id']}/file", headers=self.headers()
        )
        self.assertEqual(
            response.status, 200, await response.text() if response.status != 200 else ""
        )
        self.assert_private(response)
        self.assertEqual(await response.read(), self.slide_bytes)
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        self.assertIn("year-slides.pdf", response.headers["Content-Disposition"])
        paper = self.documents["year/paper"]
        response = await self.client.get(
            f"/v1/documents/{paper['id']}/file", headers=self.headers()
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(len(fixtures.WebAPITests.files(self)), 2)

    async def test_archive_visibility_still_obeys_the_configured_upload_channel(self):
        ids = [self.documents[f"year/{kind}"]["id"] for kind in ("slides", "paper")]
        with self.bot.store.db:
            self.bot.store.db.executemany(
                "UPDATE documents SET source_channel_id=9999 WHERE id=?",
                [(value,) for value in ids],
            )
        response = await self.client.get("/v1/presentations", headers=self.headers())
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["total"], 3)
        self.assertNotIn(
            f"archive:{self.groups['year']}",
            [item["session"]["record_key"] for item in data["presentations"]],
        )
        response = await self.client.get("/v1/archive", headers=self.headers())
        self.assertTrue(
            set(ids).isdisjoint(item["id"] for item in (await response.json())["documents"])
        )
        response = await self.client.get(f"/v1/documents/{ids[0]}/file", headers=self.headers())
        self.assertEqual(response.status, 404)
