from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from pypdf import PdfWriter

from seminar_bot.__main__ import instance_lock
from seminar_bot.archive import extract_saved
from seminar_bot.bot import DocumentKindButton, SeminarBot, classification_view, safe
from seminar_bot.documents import DocumentInfo
from seminar_bot.settings import Settings
from seminar_bot.store import UserError
from seminar_bot.submission_access import SubmissionAccess
from seminar_bot.web_api import APIError, SeminarAPI

DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 0, tzinfo=UTC)


def pdf_bytes(title="한글 연구 논문 Attention Study"):
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": title})
    writer.write(stream)
    return stream.getvalue()


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bot = SeminarBot(
            Settings(
                "test-token-not-used",
                Path(self.temp.name),
                rotation_file=Path(self.temp.name) / "rotation.json",
            )
        )
        self.bot.store.configure(1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.bot.store.set_rotation(1, [101, 102], DAY, DAY)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    def write_rotation(self, ids=(102, 101)):
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {"guilds": {"1": {"presenters": [{"user_id": str(user_id)} for user_id in ids]}}}
            )
        )

    def configuration_interaction(self, guild_id=1, channel_id=1001):
        target = SimpleNamespace(
            id=channel_id,
            guild=SimpleNamespace(id=guild_id),
            mention=f"<#{channel_id}>",
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True
            ),
        )
        interaction = SimpleNamespace(
            guild_id=guild_id,
            guild=SimpleNamespace(me=object()),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        return interaction, target

    def enable_web_submission(self):
        self.bot.settings = replace(
            self.bot.settings,
            api_token="synthetic-api-token-" * 3,
            api_guild_id=1,
            seminar_page_url="https://example.test/internal/seminars/",
        )

    def enable_notifications(self):
        config = self.bot.store.require_config(1)
        self.bot.store.configure(
            1,
            config["channel_id"],
            config["upload_channel_id"],
            config["weekday"],
            config["morning"],
            config["evening"],
            config["timezone"],
            NOW,
            notifications_enabled=True,
            reminder_start_date=DAY,
        )

    async def test_configure_requires_korean_seminar_time_and_exposes_only_one_channel(self):
        command = self.bot.tree.get_command("설정")
        options = command.to_dict(self.bot.tree)["options"]
        option = next(item for item in options if item["name"] == "세미나시간")
        self.assertTrue(option["required"])
        self.assertNotIn("upload_channel", {item["name"] for item in options})
        interaction, target = self.configuration_interaction()
        sessions = [
            dict(row) for row in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")
        ]
        with patch.object(self.bot, "reload_rotation_file", AsyncMock()):
            await command.callback(interaction, 3, target, "14:00")
            configured = self.bot.store.require_config(1)
            self.assertEqual(
                (configured["seminar_time"], configured["morning"], configured["evening"]),
                ("14:00", "09:00", "18:00"),
            )
            response = interaction.followup.send.await_args.args[0]
            self.assertIn("매주 목요일 14:00 (Asia/Seoul)", response)
            self.assertIn("알림: 아침 09:00 / 저녁 18:00", response)
            self.assertIn("알림 상태: OFF", response)
            await command.callback(
                interaction, 3, target, "15:30", morning="08:20", evening="19:10"
            )
        configured = self.bot.store.require_config(1)
        self.assertEqual(
            (configured["seminar_time"], configured["morning"], configured["evening"]),
            ("15:30", "08:20", "19:10"),
        )
        response = interaction.followup.send.await_args.args[0]
        self.assertIn("매주 목요일 15:30 (Asia/Seoul)", response)
        self.assertIn("알림: 아침 08:20 / 저녁 19:10", response)
        self.assertEqual(
            [dict(row) for row in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")],
            sessions,
        )
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_first_configuration_initializes_internal_scope_to_the_single_notification_channel(
        self,
    ):
        interaction, target = self.configuration_interaction(guild_id=2, channel_id=2001)
        with patch.object(self.bot, "reload_rotation_file", AsyncMock()):
            await self.bot.tree.get_command("설정").callback(interaction, 3, target, "14:00")
        config = self.bot.store.require_config(2)
        self.assertEqual(
            (config["channel_id"], config["upload_channel_id"], config["seminar_time"]),
            (2001, 2001, "14:00"),
        )
        text = interaction.followup.send.await_args.args[0]
        self.assertIn("매주 목요일 14:00 (Asia/Seoul)", text)
        self.assertIn("알림: 아침 09:00 / 저녁 18:00", text)
        self.assertNotIn("자료:", text)
        self.assertFalse(config["notifications_enabled"])
        self.assertIsNone(config["reminder_start_date"])

    async def test_configuration_toggle_is_explicit_preserves_start_date_and_never_resets_schedule(
        self,
    ):
        command = self.bot.tree.get_command("설정")
        options = {item["name"]: item for item in command.to_dict(self.bot.tree)["options"]}
        self.assertFalse(options["알림"].get("required", False))
        self.assertEqual(
            {item["name"]: item["value"] for item in options["알림"]["choices"]},
            {"끄기": "off", "켜기": "on"},
        )
        self.assertFalse(options["알림시작일"].get("required", False))
        interaction, target = self.configuration_interaction()
        before = "\n".join(self.bot.store.db.iterdump())
        with patch.object(self.bot, "reload_rotation_file", AsyncMock()):
            for arguments in ({"notifications": "on"}, {"reminder_start_date": "not-a-date"}):
                with self.subTest(arguments=arguments), self.assertRaises(UserError):
                    await command.callback(interaction, 3, target, "14:00", **arguments)
                self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
            calendar = [
                dict(row) for row in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")
            ]
            await command.callback(
                interaction, 3, target, "14:00", notifications="on", reminder_start_date=str(DAY)
            )
            for arguments, enabled in (
                ({}, True),
                ({"notifications": "off"}, False),
                ({}, False),
                ({"notifications": "on"}, True),
            ):
                await command.callback(interaction, 3, target, "14:00", **arguments)
                config = self.bot.store.require_config(1)
                self.assertEqual(bool(config["notifications_enabled"]), enabled)
                self.assertEqual(config["reminder_start_date"], str(DAY))
                response = interaction.followup.send.await_args.args[0]
                self.assertIn("알림 상태: " + ("ON" if enabled else "OFF"), response)
                self.assertIn(str(DAY), response)
            self.assertEqual(
                [
                    dict(row)
                    for row in self.bot.store.db.execute("SELECT * FROM sessions ORDER BY id")
                ],
                calendar,
            )

    async def test_notification_channel_change_preserves_archive_scope_progress_and_submission_grants(
        self,
    ):
        record, path = await self.retained_slides_fixture()
        self.bot.store.postpone(1, DAY, DAY + timedelta(weeks=2))
        session = self.bot.store.session_on(1, DAY)
        self.bot.store.mark_sent(session["id"], str(DAY), "morning", 901)
        with self.bot.store.db:
            self.bot.store.remember_rotation_file(1, "ab" * 32)
        access = SubmissionAccess(self.bot.store)
        invite, _ = access.issue(1, session["id"], 101, now=1_700_000_000, scope="presenter")
        token, _ = access.redeem(invite, 1, now=1_700_000_001)
        self.enable_notifications()
        self.enable_web_submission()
        settings = self.bot.settings
        tables = (
            "sessions",
            "holidays",
            "documents",
            "deliveries",
            "rotation_file_state",
            "submission_invitations",
            "submission_grants",
        )

        def history():
            return {
                table: [
                    dict(row)
                    for row in self.bot.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
                for table in tables
            }

        original = history()
        before = self.bot.store.require_config(1)
        progress = self.bot.store.submission(1, session["id"])
        content = path.read_bytes()
        interaction, target = self.configuration_interaction(channel_id=1009)
        with patch.object(self.bot, "reload_rotation_file", AsyncMock()):
            await self.bot.tree.get_command("설정").callback(interaction, 3, target, "14:00")
        self.assertEqual(
            self.bot.store.require_config(1),
            {**before, "channel_id": 1009, "seminar_time": "14:00"},
        )
        self.assertEqual(history(), original)
        self.assertEqual(self.bot.store.submission(1, session["id"]), progress)
        self.assertEqual(self.bot.store.document(1, record["id"])["source_channel_id"], 1002)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(access.validate(token, 1, now=1_700_000_002)["upload_channel_id"], 1002)
        self.assertIs(self.bot.settings, settings)

    async def test_web_submission_notification_uses_main_channel_and_keeps_archive_scope(self):
        self.enable_notifications()
        main = Mock(spec=discord.TextChannel)
        main.guild = SimpleNamespace(id=1)
        main.send = AsyncMock(return_value=SimpleNamespace(id=902))
        archive = Mock(spec=discord.TextChannel)
        archive.send = AsyncMock()
        session = self.bot.store.session_on(1, DAY)
        with patch.object(
            self.bot,
            "get_channel",
            side_effect=lambda channel_id: main if channel_id == 1001 else archive,
        ) as get:
            result = await self.bot.notify_web_submission(
                session, self.bot.store.submission(1, session["id"])
            )
        self.assertEqual(result, 902)
        get.assert_called_once_with(1001)
        main.send.assert_awaited_once()
        archive.send.assert_not_awaited()
        self.assertEqual(self.bot.store.require_config(1)["upload_channel_id"], 1002)

    async def test_default_off_suppresses_all_automatic_sends_without_mutating_existing_data(self):
        self.enable_web_submission()
        record = self.seed_legacy_document(kind="slides")
        session = self.bot.store.session_on(1, DAY)
        access = SubmissionAccess(self.bot.store)
        invitation, _ = access.issue(1, session["id"], 101, now=1_700_000_000, scope="presenter")
        token, _ = access.redeem(invitation, 1, now=1_700_000_001)
        before = "\n".join(self.bot.store.db.iterdump())
        path = Path(self.temp.name) / record["file_path"]
        content = path.read_bytes()
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel") as channel,
            patch.object(self.bot, "fetch_channel", AsyncMock()) as fetched,
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 10, 15, 9, tzinfo=UTC)
            await self.bot.reminders()
            with self.assertRaises(UserError):
                await self.bot.notify_web_submission(
                    session, self.bot.store.submission(1, session["id"])
                )
        channel.assert_not_called()
        fetched.assert_not_awaited()
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(access.validate(token, 1, now=1_700_000_002)["presenter_id"], 101)

    async def test_oldest_unsubmitted_request_is_once_per_day_and_records_actual_delivery_date(
        self,
    ):
        self.enable_web_submission()
        self.enable_notifications()
        # Complete materials in another channel must neither hide this seminar
        # nor be counted in the progress displayed to the presenter.
        self.seed_legacy_document(kind="slides")
        for index in range(1, 4):
            self.seed_legacy_document(
                attachment_id=500 + index, title=f"Hidden Source Research Paper {index}"
            )
        with self.bot.store.db:
            self.bot.store.db.execute("UPDATE documents SET source_channel_id=999")
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=903))
        first_day = date(2026, 10, 28)
        next_day = date(2026, 10, 29)
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 10, 28, 9, tzinfo=UTC)
            await self.bot.reminders()
            await self.bot.reminders()
            self.assertEqual(channel.send.await_count, 1)
            first_text = channel.send.await_args.args[0]
            self.assertIn(str(DAY), first_text)
            self.assertNotIn("오늘", first_text)
            self.assertIn("현재: 발표 자료 0개", first_text)
            self.assertIn("논문 0", first_text)
            clock.now.return_value = datetime(2026, 10, 29, 9, tzinfo=UTC)
            await self.bot.reminders()
            await self.bot.reminders()
        self.assertEqual(channel.send.await_count, 2)
        second_text = channel.send.await_args.args[0]
        self.assertIn(str(DAY + timedelta(weeks=1)), second_text)
        deliveries = [
            dict(row)
            for row in self.bot.store.db.execute(
                "SELECT session_id,on_date,kind FROM deliveries ORDER BY on_date"
            )
        ]
        self.assertEqual(
            deliveries,
            [
                {
                    "session_id": self.bot.store.session_on(1, DAY)["id"],
                    "on_date": str(first_day),
                    "kind": "evening",
                },
                {
                    "session_id": self.bot.store.session_on(1, DAY + timedelta(weeks=1))["id"],
                    "on_date": str(next_day),
                    "kind": "evening",
                },
            ],
        )

    async def test_schedule_displays_seminar_time_on_presentations_but_not_cancelled_week(self):
        self.bot.store.configure(
            1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW, seminar_time="14:00"
        )
        self.bot.store.postpone(1, DAY)
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch("seminar_bot.bot.local_now", return_value=NOW),
        ):
            await self.bot.tree.get_command("일정").callback(interaction)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        entries = embed.description.splitlines()
        presentations = [line for line in entries if "휴강" not in line]
        self.assertTrue(presentations)
        self.assertTrue(all("14:00" in line for line in presentations))
        holiday = next(line for line in entries if "휴강" in line)
        self.assertIn(DAY.isoformat(), holiday)
        self.assertNotIn("14:00", holiday)
        self.assertIn((DAY + timedelta(days=7)).isoformat(), presentations[0])
        for text in ("매주 목요일", "14:00", "Asia/Seoul", "아침 09:00", "저녁 18:00"):
            self.assertIn(text, embed.footer.text)

    async def test_schedule_without_seminar_time_has_explicit_unset_footer(self):
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch("seminar_bot.bot.local_now", return_value=NOW),
        ):
            await self.bot.tree.get_command("일정").callback(interaction)
        embed = interaction.followup.send.await_args.kwargs["embed"]
        self.assertIn("시작 시간 미설정", embed.footer.text)
        self.assertIn("Asia/Seoul", embed.footer.text)
        self.assertNotIn("None", embed.description + embed.footer.text)

    async def test_schedule_includes_only_recorded_recent_past_without_creating_history(self):
        self.bot.store.ensure_horizon(1, DAY + timedelta(days=6))
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        before = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(
                self.bot,
                "presenter_names",
                AsyncMock(return_value={101: "Presenter A", 102: "Presenter B"}),
            ),
            patch("seminar_bot.bot.local_now", return_value=NOW + timedelta(days=6)),
        ):
            await self.bot.tree.get_command("일정").callback(interaction, 2)
        lines = interaction.followup.send.await_args.kwargs["embed"].description.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], f"**{DAY}** · Presenter A (지난 발표)")
        self.assertEqual(lines[1], f"**{DAY + timedelta(weeks=1)}** · Presenter B")
        self.assertNotIn("지난 발표", lines[1])
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
        with self.bot.store.db:
            self.bot.store.db.execute(
                "DELETE FROM sessions WHERE seminar_date < ?", (str(DAY + timedelta(days=6)),)
            )
        no_history = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(
                self.bot,
                "presenter_names",
                AsyncMock(return_value={101: "Presenter A", 102: "Presenter B"}),
            ),
            patch("seminar_bot.bot.local_now", return_value=NOW + timedelta(days=6)),
        ):
            await self.bot.tree.get_command("일정").callback(interaction, 2)
        description = interaction.followup.send.await_args.kwargs["embed"].description
        self.assertNotIn("지난 발표", description)
        self.assertNotIn(str(DAY), description)
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), no_history)

    async def test_schedule_uses_nicknames_then_json_names_without_mentions_or_state_changes(self):
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {
                    "presenters": [
                        {"user_id": "101", "name": "File Name A"},
                        {"user_id": "102", "name": "File Name B"},
                    ]
                }
            )
        )
        guild = SimpleNamespace(
            get_member=lambda user_id: (
                SimpleNamespace(display_name="Server Nickname") if user_id == 101 else None
            ),
            fetch_member=AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        before = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=guild),
            patch("seminar_bot.bot.local_now", return_value=NOW),
        ):
            await self.bot.tree.get_command("일정").callback(interaction, 4)
        sent = interaction.followup.send.await_args.kwargs
        description = sent["embed"].description
        self.assertIn("Server Nickname", description)
        self.assertIn("File Name B", description)
        self.assertNotIn("File Name A", description)
        self.assertNotIn("<@", description)
        self.assertEqual(sent["allowed_mentions"].to_dict()["parse"], [])
        guild.fetch_member.assert_not_awaited()
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_schedule_fetches_each_missing_presenter_once_and_escapes_display_names(self):
        self.bot.settings.rotation_file.write_text(
            '{"presenters":[{"user_id":"101"},{"user_id":"102"}]}'
        )
        guild = SimpleNamespace(
            get_member=Mock(return_value=None),
            fetch_member=AsyncMock(
                side_effect=lambda user_id: SimpleNamespace(
                    display_name="**Name**\n@everyone <@123456789012345678>"
                    if user_id == 101
                    else "Fetched Name"
                )
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=guild),
            patch("seminar_bot.bot.local_now", return_value=NOW),
        ):
            await self.bot.tree.get_command("일정").callback(interaction, 8)
        description = interaction.followup.send.await_args.kwargs["embed"].description
        self.assertEqual(len(description.splitlines()), 8)
        self.assertIn(safe("**Name** @everyone <@123456789012345678>", 100), description)
        self.assertIn("Fetched Name", description)
        self.assertNotIn("<@123456789012345678>", description)
        self.assertNotIn("@everyone", description)
        self.assertEqual([c.args[0] for c in guild.fetch_member.await_args_list], [101, 102])

    async def test_presenter_name_failure_keeps_schedule_available_and_json_name_edits_refresh(
        self,
    ):
        guild = SimpleNamespace(
            get_member=Mock(return_value=None),
            fetch_member=AsyncMock(
                side_effect=discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"), "missing member"
                )
            ),
        )
        before = "\n".join(self.bot.store.db.iterdump())
        with patch.object(self.bot, "get_guild", return_value=guild):
            self.bot.settings.rotation_file.write_text("{")
            for error in (guild.fetch_member.side_effect, ConnectionResetError(), TimeoutError()):
                with self.subTest(error=type(error).__name__):
                    guild.fetch_member.side_effect = error
                    self.assertIn("이름 확인 불가", (await self.bot.presenter_names(1, {101}))[101])
            self.bot.settings.rotation_file.write_text(
                '{"presenters":[{"user_id":"101","name":"Original Name"}]}'
            )
            self.assertEqual(await self.bot.presenter_names(1, {101}), {101: "Original Name"})
            self.bot.settings.rotation_file.write_text(
                '{"presenters":[{"user_id":"101","name":"Updated Name"}]}'
            )
            self.assertEqual(await self.bot.presenter_names(1, {101}), {101: "Updated Name"})
        self.assertEqual(guild.fetch_member.await_count, 3)
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_seminar_time_in_reminders_keeps_trigger_times_and_duplicate_protection(self):
        self.enable_web_submission()
        self.bot.store.configure(
            1, 1001, 1002, 3, "08:20", "19:10", "Asia/Seoul", NOW, seminar_time="14:00"
        )
        self.enable_notifications()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=999))
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            for stamp, sent in (
                (datetime(2026, 10, 14, 23, 19, tzinfo=UTC), 0),
                (datetime(2026, 10, 14, 23, 20, tzinfo=UTC), 1),
                (datetime(2026, 10, 14, 23, 20, tzinfo=UTC), 1),
                (datetime(2026, 10, 15, 5, 0, tzinfo=UTC), 1),
                (datetime(2026, 10, 15, 10, 9, tzinfo=UTC), 1),
                (datetime(2026, 10, 15, 10, 10, tzinfo=UTC), 2),
                (datetime(2026, 10, 15, 10, 10, tzinfo=UTC), 2),
            ):
                clock.now.return_value = stamp
                await self.bot.reminders()
                self.assertEqual(channel.send.await_count, sent, stamp)
            self.bot.store.configure(
                1, 1001, 1002, 3, "08:20", "19:10", "Asia/Seoul", NOW, seminar_time="15:00"
            )
            await self.bot.reminders()
            self.assertEqual(channel.send.await_count, 2)
        morning, evening = channel.send.await_args_list
        self.assertIn("☀️", morning.args[0])
        self.assertIn("🌙", evening.args[0])
        for sent in (morning, evening):
            self.assertIn("14:00 (Asia/Seoul)", sent.args[0])
            self.assertIn("<@101>", sent.args[0])
            mentions = sent.kwargs["allowed_mentions"].to_dict()
            self.assertEqual(mentions["users"], [101])
            self.assertEqual(mentions["parse"], [])

    async def test_rotation_command_is_replaced_by_file(self):
        self.assertIsNone(self.bot.tree.get_command("순서설정"))
        self.assertEqual(len(self.bot.tree.get_commands()), 11)

    async def help_response(self):
        interaction = SimpleNamespace(
            guild_id=1, response=SimpleNamespace(send_message=AsyncMock())
        )
        await self.bot.tree.get_command("도움말").callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        return interaction.response.send_message.await_args

    async def test_help_is_a_single_private_message_within_the_discord_content_limit(self):
        response = await self.help_response()
        self.assertTrue(response.kwargs["ephemeral"])
        text = response.args[0]
        self.assertIsInstance(text, str)
        self.assertGreater(len(text), 0)
        self.assertLessEqual(len(text), 2000)

    async def test_help_explains_current_seminar_submission_and_archive_workflows(self):
        self.enable_web_submission()
        response = await self.help_response()
        text = response.args[0].replace("*", "").replace("`", "")
        for feature in (
            "/설정",
            "세미나시간",
            "아침",
            "저녁",
            "config/rotation.json",
            "presenters",
            "/일정",
            "휴강",
            "/제출",
            "예정",
            "수정",
            "PDF/PPT/PPTX",
            "1개 이상",
            "서로 다른 논문 3편 이상",
            "자동",
            "NAS",
            "세미나 자료 조회",
            "다운로드",
            "/검색",
        ):
            with self.subTest(feature=feature):
                self.assertIn(feature, text)
        self.assertRegex(text, r"서버\s*ID\s*(?:없|불필요|입력하지)")
        self.assertRegex(text, r"(?:과거|지난)[^\n]{0,10}예정")
        self.assertRegex(text, r"(?:수동|직접)\s*입력")
        self.assertRegex(text, r"아침[^\n]*저녁[^\n]*별도")
        schedule = next(line for line in text.splitlines() if line.startswith("/일정"))
        self.assertIn("시작", schedule)
        self.assertIn("휴강", schedule)
        self.assertRegex(text, r"제목(?:만)?[^\n]{0,15}필수")
        self.assertRegex(text, r"(?:나머지|학회|연도|DOI|링크)[^\n]{0,80}선택")
        self.assertRegex(
            text, r"논문[^\n]*(?:PDF|원본)[^\n]*(?:미보관|보관하지|저장하지|보관\s*안)"
        )
        self.assertNotIn("kind:전체", text)
        configure = next(line for line in text.splitlines() if line.startswith("/설정"))
        self.assertIn("필수", configure)
        self.assertNotIn("자료 채널", configure)
        self.assertNotIn("자동 등록", text)
        self.assertNotIn("upload_channel", text)
        for term in ("OFF", "알림시작일", "하루 1건", "회차별 한 번"):
            self.assertIn(term, text)

    async def test_help_without_web_configuration_never_advertises_discord_attachment_submission(
        self,
    ):
        text = (await self.help_response()).args[0]
        self.assertNotIn("자동 등록", text)
        self.assertNotIn("파일 첨부", text)
        self.assertNotRegex(text, r"(?:자료\s*채널|채널)에[^\n]*(?:올리|업로드|공유)")

    def assert_latest_guild_commands(self, guild_id):
        commands = self.bot.tree.get_commands(guild=discord.Object(id=guild_id))
        self.assertEqual(len(commands), 11)
        self.assertEqual(
            {command.name for command in commands},
            {command.name for command in self.bot.tree.get_commands()},
        )
        settings = next(command for command in commands if command.name == "설정")
        self.assertIn(
            "세미나시간", {option["name"] for option in settings.to_dict(self.bot.tree)["options"]}
        )

    async def test_command_sync_respects_explicit_guild_without_global_or_other_guild_requests(
        self,
    ):
        self.bot.settings = replace(self.bot.settings, guild_id=9)
        settings = self.bot.settings

        async def sync(*, guild=None):
            self.assertEqual(guild.id, 9)
            self.assert_latest_guild_commands(9)
            return []

        with (
            patch.object(
                self.bot.tree, "copy_global_to", wraps=self.bot.tree.copy_global_to
            ) as copy,
            patch.object(self.bot.tree, "sync", AsyncMock(side_effect=sync)) as synced,
            patch.object(self.bot.tree, "fetch_commands", AsyncMock()) as fetched,
        ):
            await self.bot.sync_commands()
        copy.assert_called_once()
        self.assertEqual(copy.call_args.kwargs["guild"].id, 9)
        synced.assert_awaited_once()
        fetched.assert_not_awaited()
        self.assertIs(self.bot.settings, settings)
        self.assertEqual((settings.guild_id, settings.api_guild_id), (9, None))
        self.assertEqual(self.bot.tree.get_commands(guild=discord.Object(id=1)), [])

    async def test_seminar_guild_overrides_old_command_target_and_stale_db_servers(self):
        self.bot.settings = replace(self.bot.settings, guild_id=9, api_guild_id=2)
        before = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(self.bot.tree, "sync", AsyncMock(return_value=[])) as synced,
            patch.object(self.bot.tree, "fetch_commands", AsyncMock()) as fetched,
        ):
            await self.bot.sync_commands()
        self.assertEqual(synced.await_args.kwargs["guild"].id, 2)
        fetched.assert_not_awaited()
        self.assert_latest_guild_commands(2)
        self.assertEqual(self.bot.tree.get_commands(guild=discord.Object(id=9)), [])
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_commands_reject_inactive_servers_before_any_state_change(self):
        self.bot.settings = replace(self.bot.settings, api_guild_id=1)
        before = "\n".join(self.bot.store.db.iterdump())
        for guild_id in (2, None):
            interaction = SimpleNamespace(
                guild_id=guild_id,
                response=SimpleNamespace(send_message=AsyncMock()),
            )
            self.assertFalse(await self.bot.tree.interaction_check(interaction))
            interaction.response.send_message.assert_awaited_once()
            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertTrue(await self.bot.tree.interaction_check(SimpleNamespace(guild_id=1)))
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_global_command_sync_refreshes_only_existing_guild_scopes_in_sorted_unique_targets(
        self,
    ):
        events = []

        async def sync(*, guild=None):
            events.append(("sync", guild.id if guild else None))
            if guild:
                self.assert_latest_guild_commands(guild.id)
            return []

        async def fetch(*, guild):
            events.append(("fetch", guild.id))
            return [] if guild.id == 1 else [SimpleNamespace(name="설정")]

        with (
            patch.object(
                self.bot.store,
                "all_configs",
                return_value=[
                    {"guild_id": 3},
                    {"guild_id": 2},
                    {"guild_id": 1},
                    {"guild_id": 3},
                ],
            ),
            patch.object(
                self.bot.tree, "copy_global_to", wraps=self.bot.tree.copy_global_to
            ) as copy,
            patch.object(self.bot.tree, "sync", AsyncMock(side_effect=sync)),
            patch.object(self.bot.tree, "fetch_commands", AsyncMock(side_effect=fetch)) as fetched,
        ):
            await self.bot.sync_commands()
        self.assertEqual(
            events,
            [
                ("sync", None),
                ("fetch", 1),
                ("fetch", 2),
                ("sync", 2),
                ("fetch", 3),
                ("sync", 3),
            ],
        )
        self.assertEqual(fetched.await_count, 3)
        self.assertEqual([call.kwargs["guild"].id for call in copy.call_args_list], [2, 3])
        self.assertEqual(self.bot.tree.get_commands(guild=discord.Object(id=1)), [])
        self.assertIsNone(self.bot.settings.api_guild_id)

    async def test_command_sync_continues_after_individual_guild_fetch_and_sync_http_errors(self):
        for guild_id in (2, 3):
            self.bot.store.configure(guild_id, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        events = []

        def forbidden():
            return discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "denied")

        async def sync(*, guild=None):
            events.append(("sync", guild.id if guild else None))
            if guild and guild.id == 2:
                raise forbidden()
            if guild:
                self.assert_latest_guild_commands(guild.id)
            return []

        async def fetch(*, guild):
            events.append(("fetch", guild.id))
            if guild.id == 1:
                raise forbidden()
            return [SimpleNamespace(name="old-command")]

        with (
            patch.object(
                self.bot.tree, "copy_global_to", wraps=self.bot.tree.copy_global_to
            ) as copy,
            patch.object(self.bot.tree, "sync", AsyncMock(side_effect=sync)),
            patch.object(self.bot.tree, "fetch_commands", AsyncMock(side_effect=fetch)),
            self.assertLogs("seminar_bot.bot", level="WARNING") as logs,
        ):
            await self.bot.sync_commands()
        self.assertEqual(
            events,
            [
                ("sync", None),
                ("fetch", 1),
                ("fetch", 2),
                ("sync", 2),
                ("fetch", 3),
                ("sync", 3),
            ],
        )
        self.assertEqual([call.kwargs["guild"].id for call in copy.call_args_list], [2, 3])
        self.assertEqual(len(logs.output), 2)
        self.assert_latest_guild_commands(3)

    async def test_global_command_sync_failure_propagates_without_touching_guild_scopes(self):
        failure = discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "retry")
        with (
            patch.object(
                self.bot.tree, "copy_global_to", wraps=self.bot.tree.copy_global_to
            ) as copy,
            patch.object(self.bot.tree, "sync", AsyncMock(side_effect=failure)) as synced,
            patch.object(self.bot.tree, "fetch_commands", AsyncMock()) as fetched,
            self.assertRaises(discord.HTTPException) as raised,
        ):
            await self.bot.sync_commands()
        self.assertIs(raised.exception, failure)
        synced.assert_awaited_once_with()
        copy.assert_not_called()
        fetched.assert_not_awaited()

    async def test_schedule_acknowledges_before_json_member_validation(self):
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def reload():
            interaction.response.defer.assert_awaited_once()

        with patch.object(self.bot, "reload_rotation_file", side_effect=reload):
            await self.bot.tree.get_command("일정").callback(interaction)
        self.assertIn("embed", interaction.followup.send.await_args.kwargs)

    async def test_json_hot_reload_validates_members_and_applies_order(self):
        self.write_rotation()
        guild = SimpleNamespace(
            get_member=Mock(return_value=None),
            fetch_member=AsyncMock(return_value=SimpleNamespace(bot=False)),
        )
        with (
            patch.object(self.bot, "get_guild", return_value=guild),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reload_rotation_file()
            self.assertEqual(self.bot.store.session_on(1, DAY)["presenter_id"], 102)
            self.assertEqual(guild.fetch_member.await_count, 2)
            self.bot.store.postpone(1, DAY)
            await self.bot.reload_rotation_file()
            self.assertIsNone(self.bot.store.session_on(1, DAY))
            self.assertEqual(guild.fetch_member.await_count, 2)

    async def test_invalid_json_keeps_existing_schedule_and_logs_once(self):
        self.bot.settings.rotation_file.write_text("{")
        before = self.bot.store.upcoming(1, DAY)
        with self.assertLogs("seminar_bot.bot", level="WARNING") as logs:
            await self.bot.reload_rotation_file()
            await self.bot.reload_rotation_file()
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.bot.store.upcoming(1, DAY), before)

    async def test_empty_or_other_guild_json_reports_missing_guild_once_without_changing_db(self):
        for guilds in ({}, {"2": {"presenters": [{"user_id": "201"}]}}):
            with self.subTest(guilds=guilds):
                self.bot.rotation_errors.clear()
                self.bot.settings.rotation_file.write_text(json.dumps({"guilds": guilds}))
                before = "\n".join(self.bot.store.db.iterdump())
                with (
                    patch.object(self.bot, "get_guild", return_value=None),
                    self.assertLogs("seminar_bot.bot", level="WARNING") as logs,
                ):
                    await self.bot.reload_rotation_file()
                    await self.bot.reload_rotation_file()
                self.assertIn("config/rotation.json", self.bot.rotation_errors[1])
                self.assertIn("이 서버(1)", self.bot.rotation_errors[1])
                self.assertEqual(sum("for guild 1;" in entry for entry in logs.output), 1)
                self.assertEqual(len(logs.output), 1 if not guilds else 2)
                self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_missing_guild_error_clears_on_apply_and_unchanged_file_preserves_postponement(
        self,
    ):
        self.bot.settings.rotation_file.write_text('{"guilds":{}}')
        with self.assertLogs("seminar_bot.bot", level="WARNING"):
            await self.bot.reload_rotation_file()
        self.assertIn(1, self.bot.rotation_errors)
        self.write_rotation()
        guild = SimpleNamespace(get_member=Mock(return_value=SimpleNamespace(bot=False)))
        with (
            patch.object(self.bot, "get_guild", return_value=guild),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reload_rotation_file()
        self.assertNotIn(1, self.bot.rotation_errors)
        self.assertEqual(self.bot.store.session_on(1, DAY)["presenter_id"], 102)
        self.bot.store.postpone(1, DAY)
        postponed = "\n".join(self.bot.store.db.iterdump())
        self.bot.settings.rotation_file.write_text('{"guilds":{}}')
        with self.assertLogs("seminar_bot.bot", level="WARNING"):
            await self.bot.reload_rotation_file()
        self.write_rotation()
        with patch.object(
            self.bot, "get_guild", side_effect=AssertionError("unchanged file must not reapply")
        ):
            await self.bot.reload_rotation_file()
        self.assertNotIn(1, self.bot.rotation_errors)
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), postponed)

    async def test_flat_rotation_uses_seminar_guild_despite_stale_configs_and_dev_target(self):
        self.bot.settings = replace(self.bot.settings, guild_id=9, api_guild_id=1)
        self.bot.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.bot.store.set_rotation(2, [201, 202], DAY, DAY)
        stale_config = self.bot.store.require_config(2)
        stale_sessions = self.bot.store.upcoming(2, DAY)
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {
                    "start_date": None,
                    "presenters": [
                        {"user_id": "102", "name": "Synthetic Presenter"},
                        {"user_id": "101"},
                    ],
                }
            )
        )
        guild = SimpleNamespace(get_member=Mock(return_value=SimpleNamespace(bot=False)))
        with (
            patch.object(self.bot, "get_guild", return_value=guild) as get_guild,
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reload_rotation_file()
        self.assertEqual(self.bot.store.session_on(1, DAY)["presenter_id"], 102)
        get_guild.assert_called_once_with(1)
        self.assertEqual((self.bot.settings.guild_id, self.bot.settings.api_guild_id), (9, 1))
        self.assertFalse(self.bot.rotation_errors)
        self.assertEqual(self.bot.store.require_config(2), stale_config)
        self.assertEqual(self.bot.store.upcoming(2, DAY), stale_sessions)
        self.bot.store.postpone(1, DAY)
        postponed = "\n".join(self.bot.store.db.iterdump())
        with patch.object(
            self.bot, "get_guild", side_effect=AssertionError("unchanged file must not reapply")
        ):
            await self.bot.reload_rotation_file()
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), postponed)

    async def test_flat_rotation_waits_for_configuration_of_explicit_target(self):
        self.bot.settings.rotation_file.write_text('{"presenters":[{"user_id":"101"}]}')
        fresh = SeminarBot(
            replace(
                self.bot.settings,
                data_dir=Path(self.temp.name) / "fresh",
                guild_id=1,
                api_guild_id=1,
            )
        )
        try:
            with (
                patch.object(
                    fresh,
                    "get_guild",
                    side_effect=AssertionError("flat JSON needs actual server configuration"),
                ),
                self.assertLogs("seminar_bot.bot", level="WARNING"),
            ):
                await fresh.reload_rotation_file()
            self.assertIn("/설정", fresh.rotation_errors[1])
            self.assertEqual(fresh.store.all_configs(), [])
        finally:
            await fresh.close()

    async def test_explicit_rotation_target_never_falls_back_to_another_configured_server(self):
        self.bot.settings = replace(self.bot.settings, api_guild_id=2)
        self.bot.settings.rotation_file.write_text('{"presenters":[{"user_id":"101"}]}')
        before = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(self.bot, "get_guild") as get_guild,
            self.assertLogs("seminar_bot.bot", level="WARNING"),
        ):
            await self.bot.reload_rotation_file()
        get_guild.assert_not_called()
        self.assertIn("/설정", self.bot.rotation_errors[2])
        self.assertNotIn(None, self.bot.rotation_errors)
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

    async def test_explicit_rotation_target_ignores_other_legacy_guild_entries(self):
        self.bot.settings = replace(self.bot.settings, api_guild_id=1)
        self.bot.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {
                    "guilds": {
                        "1": {"presenters": [{"user_id": "102"}]},
                        "2": {"presenters": [{"user_id": "201"}]},
                    }
                }
            )
        )
        self.bot.rotation_errors[2] = "Old inactive server error"
        guild = SimpleNamespace(get_member=Mock(return_value=SimpleNamespace(bot=False)))
        stale = self.bot.store.require_config(2)
        with (
            patch.object(self.bot, "get_guild", return_value=guild) as get_guild,
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reload_rotation_file()
        get_guild.assert_called_once_with(1)
        self.assertFalse(self.bot.rotation_errors)
        self.assertEqual(self.bot.store.session_on(1, DAY)["presenter_id"], 102)
        self.assertEqual(self.bot.store.require_config(2), stale)
        self.assertEqual(self.bot.store.upcoming(2, DAY), [])

    async def test_flat_rotation_rejects_multiple_configs_but_legacy_mapping_still_applies(self):
        self.bot.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.bot.settings.rotation_file.write_text('{"presenters":[{"user_id":"102"}]}')
        before = "\n".join(self.bot.store.db.iterdump())
        with (
            patch.object(
                self.bot,
                "get_guild",
                side_effect=AssertionError("ambiguous flat rotation must not resolve a guild"),
            ),
            self.assertLogs("seminar_bot.bot", level="WARNING"),
        ):
            await self.bot.reload_rotation_file()
        self.assertTrue(self.bot.rotation_errors.get(None))
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
        self.write_rotation()
        guild = SimpleNamespace(get_member=Mock(return_value=SimpleNamespace(bot=False)))
        with (
            patch.object(self.bot, "get_guild", return_value=guild),
            patch("seminar_bot.bot.datetime") as clock,
            self.assertLogs("seminar_bot.bot", level="WARNING"),
        ):
            clock.now.return_value = NOW
            await self.bot.reload_rotation_file()
        self.assertNotIn(None, self.bot.rotation_errors)
        self.assertNotIn(1, self.bot.rotation_errors)
        self.assertEqual(self.bot.store.session_on(1, DAY)["presenter_id"], 102)

    async def test_configure_shows_actual_escaped_rotation_error_and_current_guild_id(self):
        error = "Read failed: **unsafe** @everyone <@123>"
        target = SimpleNamespace(
            id=1001,
            guild=SimpleNamespace(id=1),
            mention="<#1001>",
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(me=object()),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def fail_reload():
            self.bot.rotation_errors[1] = error

        with patch.object(self.bot, "reload_rotation_file", side_effect=fail_reload):
            await self.bot.tree.get_command("설정").callback(interaction, 3, target, "14:00")
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs["ephemeral"])
        self.assertIn("현재 서버 ID: `1`", sent.args[0])
        self.assertIn(safe(error, 800), sent.args[0])
        self.assertNotIn(error, sent.args[0])
        with patch.object(
            self.bot, "reload_rotation_file", side_effect=self.bot.rotation_errors.clear
        ):
            await self.bot.tree.get_command("설정").callback(interaction, 3, target, "14:00")
        self.assertNotIn("현재 서버 ID:", interaction.followup.send.await_args.args[0])
        self.assertNotIn("⚠️", interaction.followup.send.await_args.args[0])

    async def test_empty_schedule_shows_actual_error_or_guild_mapping_guidance(self):
        with self.bot.store.db:
            self.bot.store.db.execute("DELETE FROM sessions WHERE guild_id=1")
            self.bot.store.db.execute(
                "UPDATE guilds SET rotation='[]',next_date=NULL WHERE guild_id=1"
            )
        interaction = SimpleNamespace(
            guild_id=1,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        for key in (None, 1):
            error = "Broken config: @everyone **invalid**"
            self.bot.rotation_errors = {key: error}
            with (
                patch.object(self.bot, "reload_rotation_file", AsyncMock()),
                self.assertRaises(UserError) as rejected,
            ):
                await self.bot.tree.get_command("일정").callback(interaction)
            self.assertIn("현재 서버 ID: `1`", str(rejected.exception))
            self.assertIn(safe(error, 800), str(rejected.exception))
            self.assertNotIn(error, str(rejected.exception))
        self.bot.rotation_errors.clear()
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            self.assertRaises(UserError) as rejected,
        ):
            await self.bot.tree.get_command("일정").callback(interaction)
        self.assertIn("현재 서버 ID: `1`", str(rejected.exception))
        self.assertIn("config/rotation.json의 presenters", str(rejected.exception))

    async def test_bot_presenter_is_rejected_without_changing_schedule(self):
        self.write_rotation()
        before = self.bot.store.upcoming(1, DAY)
        guild = SimpleNamespace(get_member=Mock(return_value=SimpleNamespace(bot=True)))
        with (
            patch.object(self.bot, "get_guild", return_value=guild),
            self.assertLogs("seminar_bot.bot", level="WARNING"),
        ):
            await self.bot.reload_rotation_file()
        self.assertEqual(self.bot.store.upcoming(1, DAY), before)

    async def test_nonmember_presenter_is_rejected_without_changing_schedule(self):
        self.write_rotation()
        before = self.bot.store.upcoming(1, DAY)
        error = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        guild = SimpleNamespace(
            get_member=Mock(return_value=None), fetch_member=AsyncMock(side_effect=error)
        )
        with (
            patch.object(self.bot, "get_guild", return_value=guild),
            self.assertLogs("seminar_bot.bot", level="WARNING"),
        ):
            await self.bot.reload_rotation_file()
        self.assertEqual(self.bot.store.upcoming(1, DAY), before)

    def message(self, data=None, filename="paper.pdf", attachment_id=500):
        data = data if data is not None else pdf_bytes()
        attachment = SimpleNamespace(
            id=attachment_id, filename=filename, size=len(data), read=AsyncMock(return_value=data)
        )
        message = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=101, bot=False),
            channel=SimpleNamespace(id=1002),
            attachments=[attachment],
            created_at=NOW,
            jump_url="https://discord.com/channels/1/1002/400",
            reply=AsyncMock(),
        )
        return message, attachment

    def seed_legacy_document(
        self, kind="paper", title="한글 연구 논문 Attention Study", attachment_id=500, info=None
    ):
        info = info or DocumentInfo(
            title,
            "",
            None,
            None,
            hashlib.sha256(str(attachment_id).encode()).hexdigest(),
            "pdf_metadata",
        )
        filename = f"{kind}-{attachment_id}.pdf"
        relative_path = ""
        if kind == "slides":
            path = Path(self.temp.name) / "files" / filename
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(pdf_bytes(title))
            relative_path = str(path.relative_to(Path(self.temp.name)))
        record_id, _ = self.bot.store.add_document(
            1,
            attachment_id,
            101,
            info,
            filename,
            relative_path,
            f"https://discord.com/channels/1/1002/{attachment_id}",
            NOW,
            source_channel_id=1002,
            kind=kind,
        )
        return self.bot.store.document(1, record_id)

    async def test_discord_attachments_are_never_collected_in_any_guild(self):
        self.assertFalse(self.bot.intents.message_content)
        await self.bot._async_setup_hook()
        self.bot.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        for enabled in (False, True):
            if enabled:
                self.enable_web_submission()
            for guild_id in (1, 2):
                with self.subTest(web_enabled=enabled, guild_id=guild_id):
                    message, attachment = self.message(filename="slides.pdf")
                    message.guild.id = guild_id
                    message.channel.id = 1002 if guild_id == 1 else 2002
                    oversized = SimpleNamespace(
                        filename="oversized.pdf", size=30 * 1024 * 1024, read=AsyncMock()
                    )
                    message.attachments.append(oversized)
                    before = "\n".join(self.bot.store.db.iterdump())
                    with (
                        patch.object(self.bot, "extract_document", AsyncMock()) as parser,
                        patch.object(
                            self.bot.store, "add_document", wraps=self.bot.store.add_document
                        ) as save,
                    ):
                        self.bot.dispatch("message", message)
                        await asyncio.sleep(0)
                    attachment.read.assert_not_awaited()
                    oversized.read.assert_not_awaited()
                    parser.assert_not_awaited()
                    save.assert_not_called()
                    message.reply.assert_not_awaited()
                    self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)
                    self.assertFalse(list(Path(self.temp.name).glob("files/**/*")))

    async def retained_slides_fixture(self):
        info = DocumentInfo(
            "Research Slides Reclassified As A Paper",
            "Sensitive extracted paper body",
            "10.1000/reference",
            None,
            "classification-fixture-sha",
            "pdf_text",
        )
        record = self.seed_legacy_document(kind="slides", info=info)
        return record, Path(self.temp.name) / record["file_path"]

    async def test_reclassification_command_deletes_paper_original_and_body_and_blocks_download(
        self,
    ):
        record, path = await self.retained_slides_fixture()
        self.assertTrue(path.is_file())
        interaction = SimpleNamespace(
            guild_id=1,
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        command = self.bot.tree.get_command("자료분류")
        await command.callback(interaction, record["id"], "paper")
        paper = self.bot.store.document(1, record["id"])
        self.assertEqual(
            (paper["kind"], paper["file_path"], paper["extracted_text"]), ("paper", "", "")
        )
        self.assertEqual(paper["doi"], "10.1000/reference")
        self.assertFalse(path.exists())
        self.assertIn(
            "원본은 복구되지 않습니다", interaction.response.send_message.await_args.args[0]
        )
        api = object.__new__(SeminarAPI)
        api.bot, api.guild_id, api.settings = self.bot, 1, self.bot.settings
        self.assertFalse(api.document_data(paper, {})["file_available"])
        with self.assertRaises(APIError) as denied:
            await api.download(SimpleNamespace(match_info={"document_id": str(record["id"])}))
        self.assertEqual(denied.exception.status, 404)
        await command.callback(interaction, record["id"], "slides")
        restored = self.bot.store.document(1, record["id"])
        self.assertEqual((restored["file_path"], restored["extracted_text"]), ("", ""))
        self.assertIn("/제출", interaction.response.send_message.await_args.args[0])
        self.assertIn("웹페이지", interaction.response.send_message.await_args.args[0])

    async def test_reclassification_keeps_an_original_referenced_by_another_document(self):
        record, path = await self.retained_slides_fixture()
        other_id, _ = self.bot.store.add_document(
            1,
            501,
            101,
            DocumentInfo(
                "Shared Slides", "Other slides body", None, None, "shared-sha", "pdf_text"
            ),
            "shared-slides.pdf",
            record["file_path"],
            "https://discord.com/channels/1/1002/501",
            NOW,
            kind="slides",
        )
        async with self.bot.schedule_lock:
            paper = await self.bot.classify_document_record(1, record["id"], "paper", 101, False)
        self.assertEqual((paper["file_path"], paper["extracted_text"]), ("", ""))
        self.assertTrue(path.is_file())
        other = self.bot.store.document(1, other_id)
        self.assertEqual(
            (other["kind"], other["file_path"], other["extracted_text"]),
            ("slides", record["file_path"], "Other slides body"),
        )

    async def test_failed_or_unauthorized_original_deletion_preserves_the_existing_record(self):
        record, path = await self.retained_slides_fixture()
        async with self.bot.schedule_lock:
            with self.assertRaises(UserError):
                await self.bot.classify_document_record(1, record["id"], "paper", 999, False)
            self.assertTrue(path.is_file())
            with patch.object(Path, "unlink", side_effect=PermissionError("fixture denial")):
                with self.assertRaises(UserError):
                    await self.bot.classify_document_record(1, record["id"], "paper", 101, False)
        current = self.bot.store.document(1, record["id"])
        self.assertEqual(
            (current["kind"], current["file_path"], current["extracted_text"]),
            (record["kind"], record["file_path"], record["extracted_text"]),
        )
        self.assertTrue(path.is_file())

    async def test_document_over_parser_limit_uses_metadata_without_network(self):
        self.bot.settings = Settings(
            "test-token-not-used", Path(self.temp.name), parser_url="http://document-parser:8081"
        )
        with (
            patch("seminar_bot.bot.MAX_FILE_BYTES", 3),
            patch("seminar_bot.bot.extract_remote") as remote,
        ):
            result = await self.bot.extract_document(Path("unused"), "large.pdf", b"%PDF-long")
        remote.assert_not_called()
        self.assertEqual(result.extraction_method, "filename")
        self.assertEqual(result.text, "")

    async def test_reminders_ignore_inactive_guild_even_when_it_is_connected_and_enabled(self):
        self.bot.settings = replace(self.bot.settings, api_guild_id=1)
        self.enable_notifications()
        self.bot.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.bot.store.set_rotation(2, [201], DAY, DAY)
        self.bot.store.configure(
            2,
            2001,
            2002,
            3,
            "09:00",
            "18:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=DAY,
        )
        stale = self.bot.store.require_config(2)
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=1000))
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)) as guild,
            patch.object(self.bot, "get_channel", return_value=channel) as get_channel,
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reminders()
        guild.assert_called_once_with(1)
        get_channel.assert_called_once_with(1001)
        channel.send.assert_awaited_once()
        self.assertEqual(self.bot.store.require_config(2), stale)
        self.assertIsNone(
            self.bot.store.db.execute(
                "SELECT 1 FROM deliveries d JOIN sessions s ON s.id=d.session_id WHERE s.guild_id=2"
            ).fetchone()
        )

    async def test_morning_and_evening_mention_only_presenter_and_persist(self):
        self.enable_web_submission()
        self.enable_notifications()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=999))
        with (
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            await self.bot.reminders()
            await self.bot.reminders()
            self.assertEqual(channel.send.await_count, 1)
            mention_config = channel.send.await_args.kwargs["allowed_mentions"].to_dict()
            self.assertEqual(mention_config["users"], [101])
            self.assertEqual(mention_config["parse"], [])
            self.assertNotIn("None", channel.send.await_args.args[0])
            self.assertNotIn("미설정", channel.send.await_args.args[0])
            clock.now.return_value = datetime(2026, 10, 15, 9, tzinfo=UTC)
            await self.bot.reminders()
            self.assertIn("<@101>", channel.send.await_args.args[0])
            self.assertIn("자료", channel.send.await_args.args[0])
            self.assertNotIn("None", channel.send.await_args.args[0])
            self.assertNotIn("미설정", channel.send.await_args.args[0])
            self.assertEqual(channel.send.await_count, 2)

    async def test_network_error_is_retried_without_marking_sent(self):
        self.enable_notifications()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(
            side_effect=[RuntimeError("temporary network error"), SimpleNamespace(id=999)]
        )
        with (
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = NOW
            with self.assertLogs("seminar_bot.bot", level="ERROR"):
                await self.bot.reminders()
            self.assertFalse(
                self.bot.store.was_sent(
                    self.bot.store.session_on(1, DAY)["id"], DAY.isoformat(), "morning"
                )
            )
            await self.bot.reminders()
            self.assertEqual(channel.send.await_count, 2)

    async def test_private_source_channel_is_not_searchable_without_permission(self):
        self.seed_legacy_document()
        channel = SimpleNamespace(
            id=1002,
            permissions_for=Mock(
                return_value=SimpleNamespace(view_channel=False, read_message_history=False)
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(text_channels=[channel]),
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await self.bot.tree.get_command("검색").callback(interaction, "attention")
        response = interaction.response.send_message.await_args
        self.assertIn("없습니다", response.args[0])
        self.assertTrue(response.kwargs["ephemeral"])
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, read_message_history=True
        )
        await self.bot.tree.get_command("검색").callback(interaction, "attention")
        response = interaction.response.send_message.await_args
        self.assertEqual(len(response.kwargs["embed"].fields), 1)
        self.assertTrue(response.kwargs["ephemeral"])

    async def test_parser_deadline_falls_back_to_filename(self):
        process = SimpleNamespace(
            communicate=AsyncMock(side_effect=TimeoutError),
            returncode=None,
            kill=Mock(),
            wait=AsyncMock(),
        )
        with (
            patch("seminar_bot.archive.asyncio.create_subprocess_exec", return_value=process),
            self.assertLogs("seminar_bot.archive", level="WARNING"),
        ):
            info = await extract_saved(Path("unused"), "timeout.pdf", b"data", timeout=0.01)
        self.assertEqual(info.title, "timeout")
        self.assertEqual(info.extraction_method, "filename")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_submission_status_still_reports_preexisting_legacy_materials(self):
        self.seed_legacy_document(kind="slides")
        for index in range(1, 4):
            self.seed_legacy_document(
                attachment_id=500 + index, title=f"Distinct Source Research Paper {index}"
            )
        session = self.bot.store.session_on(1, DAY)
        status = self.bot.store.submission(1, session["id"])
        self.assertEqual((status["slides"], status["papers"]), (1, 3))
        self.assertTrue(status["complete"])
        self.assertEqual(self.bot.store.search(1, "distinct", kind="paper")[1], 3)
        channel = SimpleNamespace(
            id=1002,
            permissions_for=Mock(
                return_value=SimpleNamespace(view_channel=True, read_message_history=True)
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(text_channels=[channel]),
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await self.bot.tree.get_command("제출현황").callback(interaction, DAY.isoformat())
        status_response = interaction.response.send_message.await_args
        self.assertIn("제출 완료", status_response.kwargs["embed"].description)
        self.assertTrue(status_response.kwargs["ephemeral"])

    async def test_classification_button_survives_restart_and_recalculates_progress(self):
        record = self.seed_legacy_document(kind="unknown")
        view = classification_view(record)
        self.assertTrue(view.is_persistent())
        button = next(item for item in view.children if item.kind == "slides")
        original_embed = discord.Embed(title="기존 자료")
        self.bot.store.rename_document(
            1, button.record_id, "Corrected Presentation Title", 101, False
        )
        self.bot.store.postpone(1, DAY)
        await self.bot.close()
        self.bot = SeminarBot(Settings("test-token-not-used", Path(self.temp.name)))
        interaction = SimpleNamespace(
            client=self.bot,
            guild_id=1,
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(embeds=[original_embed], edit=AsyncMock()),
        )
        match = re.fullmatch(
            r"seminar:kind:(?P<record_id>\d+):(?P<kind>slides|paper)", button.item.custom_id
        )
        restored = await DocumentKindButton.from_custom_id(interaction, button.item, match)
        await restored.callback(interaction)
        record = self.bot.store.document(1, restored.record_id)
        self.assertEqual(record["kind"], "slides")
        interaction.message.edit.assert_awaited_once()
        refreshed = interaction.message.edit.await_args.kwargs["embed"]
        self.assertEqual(refreshed.description, "Corrected Presentation Title")
        self.assertEqual(
            next(field.value for field in refreshed.fields if field.name == "발표 회차"),
            "2026-10-22",
        )
        self.assertIn("발표 자료", interaction.followup.send.await_args.args[0])
        self.assertIn("논문 3편", interaction.followup.send.await_args.args[0])

    async def test_other_member_cannot_classify_with_button(self):
        record = self.seed_legacy_document(kind="unknown")
        button = classification_view(record).children[0]
        interaction = SimpleNamespace(
            client=self.bot,
            guild_id=1,
            user=SimpleNamespace(id=999),
            response=SimpleNamespace(defer=AsyncMock(), is_done=Mock(return_value=True)),
            followup=SimpleNamespace(send=AsyncMock()),
            message=SimpleNamespace(embeds=[], edit=AsyncMock()),
        )
        await button.callback(interaction)
        self.assertEqual(self.bot.store.document(1, button.record_id)["kind"], "unknown")
        self.assertIn("자료를 올린 사람", interaction.followup.send.await_args.args[0])
        interaction.message.edit.assert_not_awaited()

    async def test_search_defaults_to_papers_and_can_include_slides(self):
        self.seed_legacy_document(kind="slides")
        channel = SimpleNamespace(
            id=1002,
            permissions_for=Mock(
                return_value=SimpleNamespace(view_channel=True, read_message_history=True)
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(text_channels=[channel]),
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await self.bot.tree.get_command("검색").callback(interaction, "attention")
        self.assertIn("없습니다", interaction.response.send_message.await_args.args[0])
        await self.bot.tree.get_command("검색").callback(interaction, "attention", kind="all")
        self.assertEqual(
            len(interaction.response.send_message.await_args.kwargs["embed"].fields), 1
        )

    async def test_evening_reminder_requests_personal_submission_link_without_shared_url_or_channel_upload(
        self,
    ):
        self.enable_web_submission()
        self.enable_notifications()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=1000))
        with (
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 10, 15, 9, tzinfo=UTC)
            await self.bot.reminders()
        text = channel.send.await_args.args[0]
        self.assertIn("PDF/PPT/PPTX 1개 이상", text)
        self.assertIn("논문 3편 이상", text)
        self.assertIn("현재: 발표 자료 0개", text)
        self.assertIn("<@101>", text)
        self.assertIn("/제출", text)
        self.assertIn("링크", text)
        self.assertNotIn(self.bot.settings.seminar_page_url, text)
        self.assertNotIn("https://", text)
        self.assertNotRegex(text, r"<#\d+>|채널에[^\n]*(?:공유|올려|첨부)")

    async def test_evening_without_web_configuration_requests_admin_setup_and_never_channel_upload(
        self,
    ):
        self.enable_notifications()
        channel = Mock(spec=discord.TextChannel)
        channel.guild = SimpleNamespace(id=1)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=1000))
        with (
            patch.object(self.bot, "reload_rotation_file", AsyncMock()),
            patch.object(self.bot, "get_guild", return_value=SimpleNamespace(id=1)),
            patch.object(self.bot, "get_channel", return_value=channel),
            patch("seminar_bot.bot.datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 10, 15, 9, tzinfo=UTC)
            await self.bot.reminders()
        text = channel.send.await_args.args[0]
        for word in ("관리자", "웹", "설정"):
            self.assertIn(word, text)
        self.assertNotIn("https://", text)
        self.assertNotRegex(text, r"<#\d+>|채널에[^\n]*(?:공유|올려|첨부)")


class RuntimeTests(unittest.TestCase):
    def test_duplicate_process_is_rejected_and_lock_released(self):
        with tempfile.TemporaryDirectory() as directory:
            with instance_lock(Path(directory)), self.assertRaises(ValueError):
                with instance_lock(Path(directory)):
                    self.fail("second process acquired the lock")
            with instance_lock(Path(directory)):
                pass

    def test_escaped_display_stays_within_discord_limits(self):
        self.assertLessEqual(len(safe("*" * 500, 190)), 190)


if __name__ == "__main__":
    unittest.main()
