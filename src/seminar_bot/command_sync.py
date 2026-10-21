from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import SeminarBot

log = logging.getLogger(__name__)


def _restore_commands(tree, commands, *, guild) -> None:
    tree.clear_commands(guild=guild)
    for command in commands:
        tree.add_command(command, guild=guild, override=True)


async def sync_commands(bot: SeminarBot) -> None:
    """Register commands in one remote scope while retaining local callbacks."""
    tree = bot.tree
    if bot.settings.active_guild_id:
        guild = discord.Object(id=bot.settings.active_guild_id)
        previous = tree.get_commands(guild=guild)
        tree.clear_commands(guild=guild)
        try:
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
        except BaseException:
            _restore_commands(tree, previous, guild=guild)
            raise
        log.info("Registered %s guild commands for guild %s", len(synced), guild.id)

        # Guild sync does not remove old global registrations. An empty global
        # sync removes them remotely, but local definitions still serve callbacks.
        definitions = tree.get_commands()
        tree.clear_commands(guild=None)
        try:
            await tree.sync()
        finally:
            _restore_commands(tree, definitions, guild=None)
        log.info("Removed stale global command registrations")
        return

    synced = await tree.sync()
    log.info("Registered %s global commands", len(synced))
    guild_ids = {config["guild_id"] for config in bot.store.all_configs()}
    for guild_id in sorted(guild_ids):
        guild = discord.Object(id=guild_id)
        try:
            existing = await tree.fetch_commands(guild=guild)
            if not existing:
                continue
            previous = tree.get_commands(guild=guild)
            tree.clear_commands(guild=guild)
            try:
                await tree.sync(guild=guild)
            except BaseException:
                _restore_commands(tree, previous, guild=guild)
                raise
        except discord.HTTPException as error:
            log.warning("Could not remove stale guild commands for guild %s: %s", guild_id, error)
            continue
        log.info("Removed stale guild command registrations for guild %s", guild_id)
