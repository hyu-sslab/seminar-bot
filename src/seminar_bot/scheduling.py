from __future__ import annotations

from datetime import datetime

from .store import Store, local_now, parse_time


def due_reminders(store: Store, config: dict, now: datetime) -> list[tuple[dict, str]]:
    """Catch up today's reminder; never replay old days or morning after evening."""
    current = local_now(config, now)
    today = current.date()
    store.ensure_horizon(config["guild_id"], today)
    session = store.session_on(config["guild_id"], today)
    if session is None:
        return []
    clock = current.time().replace(tzinfo=None)
    if clock >= parse_time(config["evening"]):
        kind = "evening"
    elif clock >= parse_time(config["morning"]):
        kind = "morning"
    else:
        return []
    if store.was_sent(session["id"], today.isoformat(), kind):
        return []
    return [(session, kind)]
