"""Correct an accidental postponement of the following week's seminar."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from .__main__ import instance_lock
from .rotation import snowflake
from .store import UserError, parse_date

ACCESS_TABLES = ("submission_invitations", "submission_grants")
ACCESS_SCOPE = "guild_id=? AND (session_id=? OR (scope='presenter' AND presenter_id=?))"


def _plan(db, guild_id, correct_date, mistaken_date, presenter_id):
    if guild_id is None:
        guilds = db.execute("SELECT guild_id FROM guilds LIMIT 2").fetchall()
        if len(guilds) != 1:
            raise UserError("대상 서버를 --guild-id로 지정해주세요.")
        guild_id = guilds[0]["guild_id"]
    config = db.execute("SELECT * FROM guilds WHERE guild_id=?", (guild_id,)).fetchone()
    if config is None:
        raise UserError("대상 서버의 기존 봇 설정을 찾을 수 없습니다.")
    if correct_date.weekday() != config["weekday"] or mistaken_date - correct_date != timedelta(
        weeks=1
    ):
        raise UserError("휴강 날짜 두 개는 정기 세미나 요일이며 7일 간격이어야 합니다.")

    dates = (str(correct_date), str(mistaken_date))
    sessions = {
        row["seminar_date"]: row
        for row in db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND seminar_date IN (?,?)", (guild_id, *dates)
        )
    }
    holidays = {
        row["seminar_date"]: row
        for row in db.execute(
            "SELECT * FROM holidays WHERE guild_id=? AND seminar_date IN (?,?)", (guild_id, *dates)
        )
    }
    result = {
        "guild_id": guild_id,
        "holiday": dates[0],
        "presentation": dates[1],
        "presenter_id": presenter_id,
    }
    already_applied = set(sessions) == {dates[1]} and set(holidays) == {dates[0]}
    if already_applied:
        if sessions[dates[1]]["presenter_id"] != presenter_id:
            raise UserError("변경할 날짜의 발표자가 지정한 ID와 다릅니다.")
        return {**result, "status": "already_correct"}, None
    if set(sessions) != {dates[0]} or set(holidays) != {dates[1]}:
        raise UserError(
            "기존 발표와 잘못 지정한 휴강일이 예상한 상태와 다릅니다. /일정에서 확인해주세요."
        )
    session = sessions[dates[0]]
    if session["presenter_id"] != presenter_id:
        raise UserError("옮길 회차의 발표자가 지정한 ID와 다릅니다.")
    if "original_date" in session.keys() and session["original_date"] is not None:
        raise UserError("개별 날짜 변경 이력이 있는 회차는 자동으로 정정할 수 없습니다.")
    session_id = session["id"]
    retained = db.execute(
        "SELECT COUNT(*) FROM documents WHERE guild_id=? AND session_id=?", (guild_id, session_id)
    ).fetchone()[0]
    scope = (guild_id, session_id, presenter_id)
    revoked = sum(
        db.execute(f"SELECT COUNT(*) FROM {table} WHERE {ACCESS_SCOPE}", scope).fetchone()[0]
        for table in ACCESS_TABLES
    )
    return {
        **result,
        "status": "ready",
        "session_id": session_id,
        "retained_documents": retained,
        "revoked_links": revoked,
        "move_reminder_start": config["reminder_start_date"] == dates[0],
    }, holidays[dates[1]]["id"]


def _backup(source: sqlite3.Connection, directory: Path) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(
        prefix="before-postponement-repair-", suffix=".sqlite3", dir=directory
    )
    os.close(descriptor)
    backup = Path(filename)
    try:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    return backup


def run_repair(
    database: Path,
    correct_date: date,
    mistaken_date: date,
    presenter_id: int,
    *,
    guild_id: int | None = None,
    apply: bool = False,
) -> dict:
    database = database.resolve()
    uri = database.as_uri()
    if not database.is_file():
        raise UserError("기존 세미나 DB를 찾을 수 없습니다. DATA_DIR를 확인해주세요.")
    if not apply:
        with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as source:
            source.row_factory = sqlite3.Row
            source.execute("BEGIN")
            result, _ = _plan(source, guild_id, correct_date, mistaken_date, presenter_id)
            return result

    with instance_lock(database.parent), closing(sqlite3.connect(uri + "?mode=rw", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        with db:
            db.execute("BEGIN IMMEDIATE")
            result, holiday_id = _plan(db, guild_id, correct_date, mistaken_date, presenter_id)
            if holiday_id is None:
                return result
            # The write reservation protects the plan and the backup from other
            # writers. A separate read connection copies the committed WAL state.
            with closing(sqlite3.connect(uri + "?mode=ro", uri=True)) as source:
                backup = _backup(source, database.parent / "backups")
            guild = result["guild_id"]
            session_id = result["session_id"]
            db.execute(
                "UPDATE sessions SET seminar_date=? WHERE guild_id=? AND id=?",
                (str(mistaken_date), guild, session_id),
            )
            db.execute(
                "UPDATE holidays SET seminar_date=? WHERE guild_id=? AND id=?",
                (str(correct_date), guild, holiday_id),
            )
            if result["move_reminder_start"]:
                db.execute(
                    "UPDATE guilds SET reminder_start_date=? WHERE guild_id=?",
                    (str(mistaken_date), guild),
                )
            for table in ACCESS_TABLES:
                db.execute(
                    f"DELETE FROM {table} WHERE {ACCESS_SCOPE}", (guild, session_id, presenter_id)
                )
            checked, _ = _plan(db, guild, correct_date, mistaken_date, presenter_id)
            expected_changes = 2 + result["revoked_links"] + int(result["move_reminder_start"])
            if checked["status"] != "already_correct" or db.total_changes != expected_changes:
                raise UserError("정정 결과가 예상한 변경과 달라 DB 작업을 취소했습니다.")
        return {**result, "status": "applied", "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser(description="잘못 지정한 휴강일을 실제 휴강일로 정정합니다.")
    parser.add_argument("--correct-holiday", required=True, help="실제 휴강일 YYYY-MM-DD")
    parser.add_argument("--mistaken-holiday", required=True, help="잘못 지정한 휴강일 YYYY-MM-DD")
    parser.add_argument("--presenter-id", required=True, help="옮길 회차의 발표자 Discord ID")
    parser.add_argument("--guild-id", help="대상 서버 Discord ID")
    parser.add_argument(
        "--apply", action="store_true", help="봇을 중지한 뒤 DB를 백업하고 반영합니다."
    )
    args = parser.parse_args()
    try:
        guild = (
            args.guild_id
            or os.getenv("SEMINAR_GUILD_ID", "").strip()
            or os.getenv("DISCORD_GUILD_ID", "").strip()
        )
        database = Path(os.getenv("DATA_DIR", "./data")) / "seminar.sqlite3"
        result = run_repair(
            database,
            parse_date(args.correct_holiday),
            parse_date(args.mistaken_holiday),
            snowflake(args.presenter_id),
            guild_id=snowflake(guild) if guild else None,
            apply=args.apply,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"휴강 정정 실패: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
