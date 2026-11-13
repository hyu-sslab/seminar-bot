from __future__ import annotations

import unittest
from datetime import timedelta

from seminar_bot.submission_access import session_version
from tests import test_web_api as web_api_fixture


class RescheduleWebAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = web_api_fixture.WebAPITests.asyncSetUp
    asyncTearDown = web_api_fixture.WebAPITests.asyncTearDown
    headers = web_api_fixture.WebAPITests.headers
    upload = web_api_fixture.WebAPITests.upload

    async def capability(self, session, presenter):
        invitation, _ = self.api.submission_access.issue(
            1, session["id"], presenter, scope="presenter"
        )
        token, _ = self.api.submission_access.redeem(invitation, 1)
        return {"X-Seminar-Submission-Token": token, "X-Seminar-Version": session_version(session)}

    async def test_uploaded_materials_follow_date_and_old_links_expire_without_affecting_another_presenter(
        self,
    ):
        old = dict(self.row)
        original = await self.capability(old, 101)
        uploaded = await self.upload(headers=original)
        self.assertEqual(uploaded.status, 201, await uploaded.text())
        document = (await uploaded.json())["document"]
        stored = self.bot.store.document(1, document["id"])
        path = self.data_dir / stored["file_path"]
        data = path.read_bytes()
        other = self.bot.store.session_on(1, self.day + timedelta(weeks=1))
        other_auth = await self.capability(other, 102)
        target = self.day + timedelta(days=1 if self.day.weekday() < 6 else -1)
        moved = self.bot.store.reschedule(1, self.day, target, self.today)
        self.assertEqual(moved["id"], old["id"])
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.bot.store.document(1, document["id"])["session_id"], old["id"])
        expired = await self.client.get("/v1/submission/context", headers=self.headers(**original))
        self.assertEqual(expired.status, 401, await expired.text())
        peer = await self.client.get("/v1/submission/context", headers=self.headers(**other_auth))
        self.assertEqual(peer.status, 200, await peer.text())
        self.assertEqual((await peer.json())["session_id"], other["id"])
        current = await self.capability(moved, 101)
        context = await self.client.get("/v1/submission/context", headers=self.headers(**current))
        self.assertEqual(context.status, 200, await context.text())
        choices = (await context.json())["sessions"]
        choice = next(row for row in choices if row["id"] == old["id"])
        self.assertEqual(choice["seminar_date"], str(target))
        self.assertEqual(choice["version"], session_version(moved))
        detail = await self.client.get(f"/v1/sessions/{old['id']}", headers=self.headers(**current))
        self.assertEqual(detail.status, 200, await detail.text())
        result = await detail.json()
        self.assertEqual(result["session"]["seminar_date"], str(target))
        self.assertEqual([d["id"] for d in result["documents"]], [document["id"]])
        stale = await self.client.post(
            f"/v1/sessions/{old['id']}/submit",
            json={},
            headers=self.headers(**{**current, "X-Seminar-Version": session_version(old)}),
        )
        self.assertEqual(stale.status, 409, await stale.text())
        self.assertEqual((await stale.json())["code"], "session_changed")
        # Returning to the original date must not revive either old capability.
        self.bot.store.reschedule(1, target, self.day, self.today)
        for auth in (original, current):
            response = await self.client.get("/v1/submission/context", headers=self.headers(**auth))
            self.assertEqual(response.status, 401, await response.text())
        peer = await self.client.get("/v1/submission/context", headers=self.headers(**other_auth))
        self.assertEqual(peer.status, 200, await peer.text())
        self.assertEqual(path.read_bytes(), data)
