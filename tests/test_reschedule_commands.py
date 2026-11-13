from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands

from seminar_bot.bot import SeminarBot
from seminar_bot.settings import Settings
from seminar_bot.store import UserError

TODAY = date(2026, 11, 13)
NOW = datetime(2026, 11, 13, 3, tzinfo=UTC)
THURSDAY = date(2026, 11, 19)
FRIDAY = date(2026, 11, 20)


class RescheduleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = SeminarBot(
            Settings(
                "synthetic-token-never-used",
                Path(self.temp.name),
                rotation_file=Path(self.temp.name) / "rotation.json",
            )
        )
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "11:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=15,
            after_minutes=60,
        )
        self.bot.store.set_rotation(1, [101, 102, 103], THURSDAY, TODAY)
        self.bot.reload_rotation_file = AsyncMock()
        self.bot.presenter_names = AsyncMock(
            return_value={101: "Presenter One", 102: "Presenter Two", 103: "Presenter Three"}
        )
        self.interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(id=1),
            permissions=discord.Permissions.none(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    def snapshot(self):
        return "\n".join(self.bot.store.db.iterdump())

    async def test_command_is_admin_only_with_two_required_korean_date_options(self):
        command = self.bot.tree.get_command("일정변경")
        self.assertTrue(command.default_permissions.manage_guild)
        options = command.to_dict(self.bot.tree)["options"]
        self.assertEqual(
            [(o["name"], o["type"], o["required"]) for o in options],
            [("기존날짜", 3, True), ("변경날짜", 3, True)],
        )
        self.assertTrue(command.checks)
        before = self.snapshot()
        with self.assertRaises(app_commands.MissingPermissions):
            for check in command.checks:
                await check(self.interaction)
        self.assertEqual(self.snapshot(), before)

    async def test_command_moves_one_occurrence_without_generating_or_renumbering_other_weeks(self):
        config = self.bot.store.require_config(1)
        rows = [dict(r) for r in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")]
        old = self.bot.store.session_on(1, THURSDAY)
        with (
            patch("seminar_bot.bot.local_now", return_value=NOW),
            patch.object(self.bot.store, "ensure_horizon", side_effect=AssertionError("generated")),
        ):
            await self.bot.tree.get_command("일정변경").callback(
                self.interaction, str(THURSDAY), str(FRIDAY)
            )
        moved = self.bot.store.session_on(1, FRIDAY)
        self.assertEqual(moved["id"], old["id"])
        self.assertEqual(moved["presenter_id"], old["presenter_id"])
        self.assertIsNone(self.bot.store.session_on(1, THURSDAY))
        after = [dict(r) for r in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")]
        self.assertEqual(
            [r for r in after if r["id"] != old["id"]], [r for r in rows if r["id"] != old["id"]]
        )
        self.assertEqual(self.bot.store.require_config(1), config)
        response = self.interaction.followup.send.await_args
        self.assertIn("2026-11-19 (목요일) → 2026-11-20 (금요일)", response.args[0])
        self.assertIn("Presenter One", response.args[0])
        self.assertIn("/제출", response.args[0])
        self.assertEqual(response.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertNotIn("<@", response.args[0])

    async def test_invalid_dates_or_unavailable_target_leave_database_unchanged(self):
        command = self.bot.tree.get_command("일정변경")
        for source, target in [
            ("bad", "2026-11-20"),
            ("2026-11-19", "2026-11-26"),
            ("2026-11-19", "2026-11-12"),
            ("2026-11-19", "2026-11-19"),
        ]:
            with self.subTest(source=source, target=target):
                before = self.snapshot()
                with (
                    patch("seminar_bot.bot.local_now", return_value=NOW),
                    self.assertRaises(UserError),
                ):
                    await command.callback(self.interaction, source, target)
                self.assertEqual(self.snapshot(), before)
        self.interaction.followup.send.assert_not_awaited()

    async def test_schedule_shows_changed_weekday_and_original_date_without_a_ping(self):
        self.bot.store.reschedule(1, THURSDAY, FRIDAY, TODAY)
        with patch("seminar_bot.bot.local_now", return_value=datetime(2026, 11, 19, 3, tzinfo=UTC)):
            await self.bot.tree.get_command("일정").callback(self.interaction, weeks=4)
        response = self.interaction.followup.send.await_args
        embed = response.kwargs["embed"]
        self.assertIn("이번 주 발표 · 2026-11-20 (금)", embed.description)
        self.assertIn("일정 변경 (기존 11/19 목)", embed.description)
        self.assertIn("기본 일정: 매주 목요일 16:00", embed.footer.text)
        self.assertNotIn("Asia/Seoul", str(embed.to_dict()))
        self.assertEqual(response.kwargs["allowed_mentions"].to_dict()["parse"], [])
