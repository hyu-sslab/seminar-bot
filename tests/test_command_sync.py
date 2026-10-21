from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
from discord import app_commands

from seminar_bot.command_sync import sync_commands


def command(name):
    async def callback(interaction: discord.Interaction):
        pass

    return app_commands.Command(name=name, description=name, callback=callback)


class RemoteCommands:
    """Model Discord's per-application bulk replacement for each command scope."""

    def __init__(self, tree):
        self.tree = tree
        self.commands = {}
        self.payloads = []
        self.fetches = []
        self.sync_errors = {}
        self.fetch_errors = {}

    async def sync(self, *, guild=None):
        scope = guild.id if guild else None
        payload = tuple(command.name for command in self.tree.get_commands(guild=guild))
        self.payloads.append((scope, payload))
        if scope in self.sync_errors:
            raise self.sync_errors[scope]
        self.commands[scope] = payload
        return [SimpleNamespace(name=name) for name in payload]

    async def fetch(self, *, guild=None):
        scope = guild.id if guild else None
        self.fetches.append(scope)
        if scope in self.fetch_errors:
            raise self.fetch_errors[scope]
        return [SimpleNamespace(name=name) for name in self.commands.get(scope, ())]


class CommandSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = discord.Client(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self.client)
        self.definitions = [command("schedule"), command("submit")]
        for definition in self.definitions:
            self.tree.add_command(definition)
        self.remote = RemoteCommands(self.tree)
        self.tree.sync = AsyncMock(side_effect=self.remote.sync)
        self.tree.fetch_commands = AsyncMock(side_effect=self.remote.fetch)
        self.store = SimpleNamespace(all_configs=Mock(return_value=[]))
        self.bot = SimpleNamespace(
            tree=self.tree, settings=SimpleNamespace(active_guild_id=7), store=self.store
        )

    async def asyncTearDown(self):
        await self.client.close()

    def assert_local_definitions(self):
        self.assertEqual(self.tree.get_commands(), self.definitions)
        for expected in self.definitions:
            self.assertIs(self.tree.get_command(expected.name), expected)

    async def test_active_guild_replaces_stale_commands_and_removes_global_duplicates(self):
        target = discord.Object(id=7)
        unrelated = discord.Object(id=9)
        old_target = command("legacy_target")
        old_unrelated = command("unrelated")
        self.tree.add_command(old_target, guild=target)
        self.tree.add_command(old_unrelated, guild=unrelated)
        self.remote.commands = {
            None: ("schedule", "submit", "legacy_global"),
            7: ("schedule", "legacy_target"),
            9: ("unrelated",),
        }

        with self.assertLogs("seminar_bot.command_sync", level="INFO") as logs:
            await sync_commands(self.bot)

        self.assertEqual(self.remote.payloads, [(7, ("schedule", "submit")), (None, ())])
        self.assertEqual(
            self.remote.commands, {None: (), 7: ("schedule", "submit"), 9: ("unrelated",)}
        )
        self.assertEqual(self.tree.get_commands(guild=target), self.definitions)
        self.assertEqual(self.tree.get_commands(guild=unrelated), [old_unrelated])
        self.assert_local_definitions()
        self.assertEqual(self.remote.fetches, [])
        self.store.all_configs.assert_not_called()
        self.assertTrue(
            any("Removed stale global command registrations" in line for line in logs.output)
        )

    async def test_repeated_startup_keeps_single_remote_scope_and_callback_identity(self):
        self.remote.commands = {None: ("legacy",), 7: ("legacy",), 9: ("untouched",)}

        await sync_commands(self.bot)
        await sync_commands(self.bot)

        self.assertEqual(
            self.remote.commands, {None: (), 7: ("schedule", "submit"), 9: ("untouched",)}
        )
        self.assertEqual(len(self.remote.payloads), 4)
        self.assert_local_definitions()

    async def test_guild_sync_failure_restores_local_scope_and_keeps_remote_globals(self):
        target = discord.Object(id=7)
        old = command("old_guild")
        self.tree.add_command(old, guild=target)
        self.remote.commands = {None: ("old_global",), 7: ("old_guild",)}
        self.remote.sync_errors[7] = RuntimeError("guild unavailable")

        with self.assertRaisesRegex(RuntimeError, "guild unavailable"):
            await sync_commands(self.bot)

        self.assertEqual(self.remote.payloads, [(7, ("schedule", "submit"))])
        self.assertEqual(self.remote.commands, {None: ("old_global",), 7: ("old_guild",)})
        self.assertEqual(self.tree.get_commands(guild=target), [old])
        self.assert_local_definitions()

    async def test_failed_global_cleanup_restores_callbacks_and_can_be_retried(self):
        self.remote.commands = {None: ("old_global",), 7: ("old_guild",)}
        self.remote.sync_errors[None] = RuntimeError("global unavailable")

        with self.assertRaisesRegex(RuntimeError, "global unavailable"):
            await sync_commands(self.bot)
        self.assertEqual(self.remote.commands[None], ("old_global",))
        self.assertEqual(self.remote.commands[7], ("schedule", "submit"))
        self.assert_local_definitions()

        self.remote.sync_errors.clear()
        await sync_commands(self.bot)

        self.assertEqual(self.remote.commands, {None: (), 7: ("schedule", "submit")})
        self.assert_local_definitions()

    async def test_cancelled_cleanup_restores_callbacks(self):
        self.remote.sync_errors[None] = asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await sync_commands(self.bot)

        self.assert_local_definitions()

    async def test_all_command_types_are_restored_after_global_cleanup(self):
        async def inspect_message(interaction: discord.Interaction, message: discord.Message):
            pass

        menu = app_commands.ContextMenu(name="inspect", callback=inspect_message)
        group = app_commands.Group(name="group", description="group")
        group.add_command(command("child"))
        self.tree.add_command(menu)
        self.tree.add_command(group)
        self.definitions = self.tree.get_commands()

        await sync_commands(self.bot)

        self.assertEqual(set(self.remote.commands[7]), {"schedule", "submit", "inspect", "group"})
        self.assertEqual(self.remote.commands[None], ())
        self.assertEqual(self.tree.get_commands(), self.definitions)
        self.assertIs(self.tree.get_command("inspect", type=discord.AppCommandType.message), menu)
        self.assertIs(self.tree.get_command("group"), group)

    async def test_global_mode_removes_only_preexisting_configured_guild_registrations(self):
        self.bot.settings.active_guild_id = None
        self.store.all_configs.return_value = [{"guild_id": 3}, {"guild_id": 2}, {"guild_id": 3}]
        old = command("old_guild")
        self.tree.add_command(old, guild=discord.Object(id=3))
        self.remote.commands = {None: ("old_global",), 2: (), 3: ("old_guild",), 9: ("unrelated",)}

        await sync_commands(self.bot)

        self.assertEqual(self.remote.payloads, [(None, ("schedule", "submit")), (3, ())])
        self.assertEqual(self.remote.fetches, [2, 3])
        self.assertEqual(
            self.remote.commands, {None: ("schedule", "submit"), 2: (), 3: (), 9: ("unrelated",)}
        )
        self.assertEqual(self.tree.get_commands(guild=discord.Object(id=3)), [])
        self.assert_local_definitions()

    async def test_global_sync_failure_does_not_touch_guild_registrations(self):
        self.bot.settings.active_guild_id = None
        self.store.all_configs.return_value = [{"guild_id": 3}]
        self.remote.commands = {None: ("old_global",), 3: ("old_guild",)}
        self.remote.sync_errors[None] = RuntimeError("global unavailable")

        with self.assertRaisesRegex(RuntimeError, "global unavailable"):
            await sync_commands(self.bot)

        self.assertEqual(self.remote.fetches, [])
        self.assertEqual(self.remote.commands, {None: ("old_global",), 3: ("old_guild",)})
        self.assert_local_definitions()

    async def test_global_mode_continues_after_inaccessible_guild_and_restores_failed_scope(self):
        self.bot.settings.active_guild_id = None
        self.store.all_configs.return_value = [{"guild_id": 2}, {"guild_id": 3}, {"guild_id": 4}]
        old = command("old_guild")
        self.tree.add_command(old, guild=discord.Object(id=3))
        self.remote.commands = {2: ("left_server",), 3: ("old_guild",), 4: ("old_guild",)}
        unavailable = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "not allowed"
        )
        self.remote.fetch_errors[2] = unavailable
        self.remote.sync_errors[3] = unavailable

        with self.assertLogs("seminar_bot.command_sync", level="WARNING"):
            await sync_commands(self.bot)

        self.assertEqual(self.remote.fetches, [2, 3, 4])
        self.assertEqual(self.remote.commands[2], ("left_server",))
        self.assertEqual(self.remote.commands[3], ("old_guild",))
        self.assertEqual(self.remote.commands[4], ())
        self.assertEqual(self.tree.get_commands(guild=discord.Object(id=3)), [old])
        self.assert_local_definitions()


if __name__ == "__main__":
    unittest.main()
