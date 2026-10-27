from __future__ import annotations

from datetime import datetime

from .store import Store, local_now, parse_time


def due_reminders(store: Store, config: dict, now: datetime) -> list[tuple[dict, str]]:
    """Select today's current stage without replaying older stages or sessions."""
    floor = config.get("reminder_start_date")
    if not config.get("notifications_enabled") or not floor:
        return []
    current = local_now(config, now)
    today = current.date()
    if floor > today.isoformat() or today.weekday() != config["weekday"]:
        return []
    clock = current.time().replace(tzinfo=None)
    if clock < parse_time(config["morning"]):
        return []

    # A late restart sends at most the current stage. Never queue the noon and
    # pre-seminar messages together when the bot comes back after the seminar.
    before = config.get("before_reminder")
    if clock >= parse_time(config["evening"]):
        kind = "evening"
    elif before is not None and clock >= parse_time(before):
        kind = "before"
    else:
        kind = "morning"

    row = store.db.execute(
        "SELECT s.* FROM sessions s WHERE s.guild_id=? AND s.seminar_date=? "
        "AND NOT EXISTS(SELECT 1 FROM holidays h WHERE h.guild_id=s.guild_id "
        "AND h.seminar_date=s.seminar_date)",
        (config["guild_id"], today.isoformat()),
    ).fetchone()
    if row is None or store.was_sent(row["id"], today.isoformat(), kind):
        return []
    if kind != "morning":
        status = store.submission(
            config["guild_id"], row["id"], visible_channel_ids=[config["upload_channel_id"]]
        )
        if status["complete"]:
            return []
    return [(dict(row), kind)]
