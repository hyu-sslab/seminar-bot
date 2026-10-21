from __future__ import annotations

from datetime import datetime

from .store import Store, local_now, parse_time


def due_reminders(store: Store, config: dict, now: datetime) -> list[tuple[dict, str]]:
    """Read recorded sessions only; request one incomplete occurrence per day."""
    floor = config.get("reminder_start_date")
    if not config.get("notifications_enabled") or not floor:
        return []
    current = local_now(config, now)
    today = current.date()
    if floor > today.isoformat():
        return []
    clock = current.time().replace(tzinfo=None)
    if clock < parse_time(config["morning"]):
        return []

    if clock < parse_time(config["evening"]):
        if today.weekday() != config["weekday"]:
            return []
        row = store.db.execute(
            "SELECT s.* FROM sessions s WHERE s.guild_id=? AND s.seminar_date=? "
            "AND NOT EXISTS(SELECT 1 FROM holidays h WHERE h.guild_id=s.guild_id "
            "AND h.seminar_date=s.seminar_date)",
            (config["guild_id"], today.isoformat()),
        ).fetchone()
        if row is None or store.was_sent(row["id"], today.isoformat(), "morning"):
            return []
        return [(dict(row), "morning")]

    sent_today = store.db.execute(
        "SELECT 1 FROM deliveries d JOIN sessions s ON s.id=d.session_id "
        "WHERE s.guild_id=? AND d.kind='evening' AND d.on_date=? LIMIT 1",
        (config["guild_id"], today.isoformat()),
    ).fetchone()
    if sent_today:
        return []
    rows = store.db.execute(
        "SELECT s.* FROM sessions s WHERE s.guild_id=? AND s.seminar_date>=? "
        "AND s.seminar_date<=? "
        "AND NOT EXISTS(SELECT 1 FROM holidays h WHERE h.guild_id=s.guild_id "
        "AND h.seminar_date=s.seminar_date) "
        "AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.session_id=s.id "
        "AND d.kind='evening') ORDER BY s.seminar_date, s.id",
        (config["guild_id"], floor, today.isoformat()),
    ).fetchall()
    for row in rows:
        status = store.submission(
            config["guild_id"], row["id"], visible_channel_ids=[config["upload_channel_id"]]
        )
        if not status["complete"]:
            return [(dict(row), "evening")]
    return []
