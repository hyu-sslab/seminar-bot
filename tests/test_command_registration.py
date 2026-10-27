from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import discord

from seminar_bot.bot import SeminarBot
from seminar_bot.settings import Settings

APPLICATION_ID = 900001
GUILD_ID = 700001
OTHER_APPLICATION_ID = 900002
EXPECTED_OPTIONS = (
    "weekday",
    "channel",
    "세미나시간",
    "일정안내",
    "알림",
    "알림시작일",
    "시작전분",
    "시작후분",
)


class CommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real CommandTree serialization and AppCommand response parsing."""

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bot = SeminarBot(
            Settings(
                token="synthetic-token-never-sent",
                data_dir=root,
                rotation_file=root / "rotation.json",
                api_guild_id=GUILD_ID,
            )
        )
        self.bot._connection.application_id = APPLICATION_ID
        self.remote = {
            (APPLICATION_ID, None): self.response(
                [
                    {
                        "name": "설정",
                        "description": "stale global settings",
                        "type": 1,
                        "options": [{"name": "morning", "description": "stale", "type": 3}],
                    }
                ],
                None,
            ),
            (APPLICATION_ID, GUILD_ID): self.response(
                [
                    {
                        "name": "설정",
                        "description": "stale guild settings",
                        "type": 1,
                        "options": [{"name": "evening", "description": "stale", "type": 3}],
                    }
                ],
                GUILD_ID,
            ),
            (OTHER_APPLICATION_ID, GUILD_ID): [{"name": "unrelated_application"}],
        }
        self.calls = []
        self.response_option_override = None
        http = self.bot.tree._http
        # Keep tree.sync itself real: these are the final SDK HTTP boundaries.
        http.bulk_upsert_guild_commands = AsyncMock(side_effect=self.replace_guild)
        http.bulk_upsert_global_commands = AsyncMock(side_effect=self.replace_global)
        http.get_guild_commands = AsyncMock(side_effect=self.fetch_guild)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    def response(self, payload, guild_id):
        rows = []
        for index, item in enumerate(payload):
            row = {
                **copy.deepcopy(item),
                "id": str(10000 + index),
                "application_id": str(APPLICATION_ID),
                "version": "1",
            }
            if guild_id is not None:
                row["guild_id"] = str(guild_id)
            if getattr(self, "response_option_override", None) and row["name"] == "설정":
                for option in row["options"]:
                    if option["name"] == "일정안내":
                        option["name"] = self.response_option_override
            rows.append(row)
        return rows

    async def replace_guild(self, application_id, guild_id, *, payload):
        self.calls.append((application_id, guild_id, copy.deepcopy(payload)))
        response = self.response(payload, guild_id)
        self.remote[(application_id, guild_id)] = response
        return response

    async def replace_global(self, application_id, *, payload):
        self.calls.append((application_id, None, copy.deepcopy(payload)))
        response = self.response(payload, None)
        self.remote[(application_id, None)] = response
        return response

    async def fetch_guild(self, application_id, guild_id):
        return copy.deepcopy(self.remote.get((application_id, guild_id), []))

    def assert_current_settings(self, commands):
        settings = [command for command in commands if command["name"] == "설정"]
        self.assertEqual(len(settings), 1)
        self.assertEqual(
            tuple(option["name"] for option in settings[0]["options"]), EXPECTED_OPTIONS
        )
        self.assertEqual(len(commands), 8)

    async def test_guild_bulk_replacement_keeps_korean_option_names_and_cleans_global_duplicates(
        self,
    ):
        with self.assertLogs("seminar_bot.command_sync", level="INFO") as logs:
            await self.bot.sync_commands()
        self.assertEqual(
            [(app, guild) for app, guild, _ in self.calls],
            [
                (APPLICATION_ID, GUILD_ID),
                (APPLICATION_ID, None),
            ],
        )
        self.assert_current_settings(self.calls[0][2])
        self.assert_current_settings(self.remote[(APPLICATION_ID, GUILD_ID)])
        self.assertEqual(self.remote[(APPLICATION_ID, None)], [])
        self.assertEqual(
            self.remote[(OTHER_APPLICATION_ID, GUILD_ID)], [{"name": "unrelated_application"}]
        )
        self.assertIn(
            "Configured /설정 options: " + ", ".join(EXPECTED_OPTIONS), "\n".join(logs.output)
        )
        for scope in (None, discord.Object(id=GUILD_ID)):
            local = self.bot.tree.get_command("설정", guild=scope)
            self.assertEqual(
                tuple(option["name"] for option in local.to_dict(self.bot.tree)["options"]),
                EXPECTED_OPTIONS,
            )

    async def test_diagnostic_reports_options_from_the_http_response(self):
        self.response_option_override = "서버응답옵션"
        with self.assertLogs("seminar_bot.command_sync", level="INFO") as logs:
            await self.bot.sync_commands()
        self.assert_current_settings(self.calls[0][2])
        line = next(line for line in logs.output if "Configured /설정 options:" in line)
        self.assertIn("서버응답옵션", line)
        self.assertNotIn("일정안내", line)

    async def test_repeated_real_tree_sync_preserves_one_settings_command_in_one_scope(self):
        await self.bot.sync_commands()
        await self.bot.sync_commands()
        self.assert_current_settings(self.remote[(APPLICATION_ID, GUILD_ID)])
        self.assertEqual(self.remote[(APPLICATION_ID, None)], [])
        self.assertEqual([guild for _, guild, _ in self.calls], [GUILD_ID, None, GUILD_ID, None])

    async def test_global_registration_reports_schema_and_removes_old_configured_guild_copy(self):
        self.bot.settings = replace(self.bot.settings, api_guild_id=None)
        self.bot.store.configure(GUILD_ID, 11, 12, 3, "12:00", "17:00", "Asia/Seoul")
        with self.assertLogs("seminar_bot.command_sync", level="INFO") as logs:
            await self.bot.sync_commands()
        self.assert_current_settings(self.remote[(APPLICATION_ID, None)])
        self.assertEqual(self.remote[(APPLICATION_ID, GUILD_ID)], [])
        self.assertEqual([guild for _, guild, _ in self.calls], [None, GUILD_ID])
        self.assertIn(
            "Configured /설정 options: " + ", ".join(EXPECTED_OPTIONS), "\n".join(logs.output)
        )
        self.assertEqual(
            self.remote[(OTHER_APPLICATION_ID, GUILD_ID)], [{"name": "unrelated_application"}]
        )
