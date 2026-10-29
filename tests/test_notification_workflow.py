from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord

from seminar_bot.bot import SeminarBot
from seminar_bot.documents import DocumentInfo
from seminar_bot.settings import Settings

DAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 27, 3, tzinfo=UTC)


class NotificationWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            token="test-discord-token-never-used",
            data_dir=Path(self.temp.name),
            rotation_file=Path(self.temp.name) / "rotation.json",
            api_token="x" * 64,
            api_guild_id=1,
            seminar_page_url="https://site.test/internal/seminars/",
        )
        self.bot = SeminarBot(self.settings)
        self.channel = Mock(spec=discord.TextChannel)
        self.channel.guild = SimpleNamespace(id=1)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=9001))
        self.mock_discord()
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=15,
            after_minutes=60,
        )
        self.bot.store.set_rotation(1, [101, 102, 103, 104, 105], DAY, NOW.date())
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=DAY,
        )
        self.session = self.bot.store.session_on(1, DAY)

    def mock_discord(self):
        self.bot.reload_rotation_file = AsyncMock()
        self.bot.get_channel = Mock(return_value=self.channel)
        self.bot.get_guild = Mock(
            return_value=SimpleNamespace(
                id=1,
                get_member=lambda user_id: SimpleNamespace(display_name=f"Presenter {user_id}"),
            )
        )

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    async def tick(self, hour, minute=0, *, day=29):
        stamp = datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("Asia/Seoul")).astimezone(UTC)
        with patch("seminar_bot.bot.datetime") as clock:
            clock.now.return_value = stamp
            await self.bot.reminders()

    def complete(self):
        for index in range(4):
            kind = "slides" if index == 0 else "paper"
            self.bot.store.add_document(
                1,
                5000 + index,
                101,
                DocumentInfo(
                    f"Synthetic Research {index}",
                    "",
                    None,
                    None,
                    hashlib.sha256(str(index).encode()).hexdigest(),
                    "user_metadata",
                ),
                f"synthetic-{index}.pdf",
                "files/synthetic.pdf" if index == 0 else "",
                "",
                NOW,
                source_channel_id=1002,
                kind=kind,
                session_id=self.session["id"],
                source_type="web" if index == 0 else "manual",
                upload_key=f"synthetic:{index}",
            )

    async def test_four_week_announcement_and_two_reminders_each_send_once_to_current_presenter(
        self,
    ):
        for hour, minute, expected in [
            (11, 59, 0),
            (12, 0, 1),
            (12, 0, 1),
            (15, 44, 1),
            (15, 45, 2),
            (15, 45, 2),
            (16, 59, 2),
            (17, 0, 3),
            (23, 59, 3),
        ]:
            await self.tick(hour, minute)
            self.assertEqual(self.channel.send.await_count, expected, (hour, minute))
        await self.tick(17, day=30)
        self.assertEqual(self.channel.send.await_count, 3)
        announcement, before, after = self.channel.send.await_args_list
        embed = announcement.kwargs["embed"]
        self.assertEqual(len(embed.description.splitlines()), 4)
        self.assertIn("🔵 **이번 주 발표 · 2026-10-29 · 16:00 · Presenter 101**", embed.description)
        for day in ("2026-11-05", "2026-11-12", "2026-11-19"):
            self.assertIn(day, embed.description)
        self.assertNotIn("2026-11-26", embed.description)
        self.assertNotIn("<@", embed.description)
        self.assertNotIn("Asia/Seoul", embed.description + embed.footer.text)
        self.assertIn("**이번 주 발표자: <@101>**", announcement.args[0])
        self.assertIn("시작 전", before.args[0])
        self.assertIn("시작 60분 후", after.args[0])
        self.assertNotIn("종료 후", after.args[0])
        for call in (announcement, before, after):
            self.assertIn("/제출", call.args[0])
            self.assertIn("16:00", call.args[0])
            self.assertNotIn("Asia/Seoul", call.args[0])
            self.assertEqual(call.args[0].count("<@101>"), 1)
            mentions = call.kwargs["allowed_mentions"].to_dict()
            self.assertEqual(mentions["users"], [101])
            self.assertEqual(mentions["parse"], [])
            self.assertLessEqual(len(call.kwargs["nonce"]), 25)
        self.assertEqual(len({call.kwargs["nonce"] for call in (announcement, before, after)}), 3)
        self.assertEqual(
            [r[0] for r in self.bot.store.db.execute("SELECT kind FROM deliveries ORDER BY rowid")],
            ["morning", "before", "evening"],
        )

    async def test_completed_materials_keep_announcement_but_skip_both_requests(self):
        self.complete()
        await self.tick(12)
        self.assertIn("제출을 확인했습니다", self.channel.send.await_args.args[0])
        await self.tick(15, 45)
        await self.tick(17)
        self.assertEqual(self.channel.send.await_count, 1)

    async def test_changing_start_time_moves_both_default_requests_without_repeating_announcement(
        self,
    ):
        await self.tick(12)
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="15:00",
        )
        for hour, minute, count in ((14, 44, 1), (14, 45, 2), (15, 45, 2), (16, 0, 3), (17, 0, 3)):
            await self.tick(hour, minute)
            self.assertEqual(self.channel.send.await_count, count)
        before, after = self.channel.send.await_args_list[1:]
        self.assertIn("15:00", before.args[0])
        self.assertIn("15:00", after.args[0])
        self.assertNotIn("Asia/Seoul", before.args[0] + after.args[0])
        self.assertIn("15분 전", before.args[0])
        self.assertIn("시작 60분 후", after.args[0])

    async def test_custom_relative_offsets_follow_the_new_start_in_the_actual_delivery_loop(self):
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=30,
            after_minutes=90,
        )
        self.bot.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:30",
            "Asia/Seoul",
            NOW,
            seminar_time="17:00",
        )
        for hour, minute, count in ((12, 0, 1), (16, 29, 1), (16, 30, 2), (18, 29, 2), (18, 30, 3)):
            await self.tick(hour, minute)
            self.assertEqual(self.channel.send.await_count, count)
        before, after = self.channel.send.await_args_list[1:]
        self.assertIn("30분 전", before.args[0])
        self.assertIn("시작 90분 후", after.args[0])
        self.assertIn("17:00", before.args[0])
        self.assertIn("17:00", after.args[0])
        self.assertNotIn("Asia/Seoul", before.args[0] + after.args[0])

    async def test_submission_between_requests_suppresses_the_after_seminar_request(self):
        await self.tick(15, 45)
        self.complete()
        await self.tick(17)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIn("시작 전", self.channel.send.await_args.args[0])

    async def test_late_restart_sends_only_current_stage_and_never_catches_up_old_sessions(self):
        with self.bot.store.db:
            self.bot.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(1,'2026-10-15',999)"
            )
        await self.tick(17, day=28)
        self.channel.send.assert_not_awaited()
        await self.tick(17, 30)
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIn("2026-10-29", self.channel.send.await_args.args[0])
        self.assertNotIn("<@999>", self.channel.send.await_args.args[0])
        self.assertEqual(
            [r[0] for r in self.bot.store.db.execute("SELECT kind FROM deliveries")], ["evening"]
        )

    async def test_delivery_records_survive_restart_and_swap_does_not_repeat_current_stage(self):
        await self.tick(12)
        first_nonce = self.channel.send.await_args.kwargs["nonce"]
        await self.bot.close()
        self.bot = SeminarBot(self.settings)
        self.mock_discord()
        self.bot.store.swap(1, DAY, date(2026, 11, 5), DAY)
        await self.tick(12, 5)
        self.assertEqual(self.channel.send.await_count, 1)
        await self.tick(15, 45)
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertIn("<@102>", self.channel.send.await_args.args[0])
        self.assertNotEqual(self.channel.send.await_args.kwargs["nonce"], first_nonce)

    async def test_retry_uses_same_nonce_when_the_discord_response_is_lost(self):
        self.channel.send.side_effect = [
            RuntimeError("synthetic uncertain delivery"),
            SimpleNamespace(id=9002),
        ]
        with self.assertLogs("seminar_bot.bot", level="ERROR"):
            await self.tick(15, 45)
        await self.tick(15, 46)
        failed, retried = self.channel.send.await_args_list
        self.assertEqual(failed.kwargs["nonce"], retried.kwargs["nonce"])
        await self.tick(15, 47)
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_stage_is_rechecked_after_slow_name_lookup(self):
        noon = datetime(2026, 10, 29, 3, tzinfo=UTC)
        after = datetime(2026, 10, 29, 8, tzinfo=UTC)
        with patch("seminar_bot.bot.datetime") as clock:
            clock.now.side_effect = [noon, after]
            await self.bot.reminders()
        self.channel.send.assert_not_awaited()
