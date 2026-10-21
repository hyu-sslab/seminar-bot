"""Align recorded seminar progress to an explicitly supplied past presentation."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .__main__ import instance_lock
from .rotation import Rotation, read_rotations, snowflake
from .store import Store, UserError, local_now, next_weekday, parse_date


def align_progress(
    store: Store,
    rotation: Rotation,
    last_date: date,
    last_presenter: int,
    today: date,
    unchanged: Callable[[], None],
) -> tuple[date, int]:
    """Keep session IDs and the canonical order; refuse conflicting history."""
    config = store.require_config(rotation.guild_id)
    if last_date > today or last_date.weekday() != config["weekday"]:
        raise UserError("기준 발표일은 오늘 이전이며 설정된 세미나 요일과 같아야 합니다.")
    if rotation.start_date is not None:
        raise UserError("이 보정은 start_date가 null인 발표 순서 파일에만 적용할 수 있습니다.")
    if last_presenter not in rotation.presenters:
        raise UserError("기준 발표자가 현재 JSON 명단에 없습니다.")
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        if store.db.execute(
            "SELECT 1 FROM holidays WHERE guild_id=? AND seminar_date>=? LIMIT 1",
            (rotation.guild_id, str(last_date)),
        ).fetchone():
            raise UserError(
                "기준일 이후에 휴강 기록이 있어 자동 보정을 중단했습니다. 기존 일정은 유지됩니다."
            )
        rows = [
            dict(row)
            for row in store.db.execute(
                "SELECT * FROM sessions WHERE guild_id=? AND seminar_date>=? ORDER BY seminar_date",
                (rotation.guild_id, str(last_date)),
            )
        ]
        old_order = json.loads(config["rotation"])
        future = [row for row in rows if row["seminar_date"] > str(last_date)]
        if future:
            if not old_order or not config["next_date"]:
                raise UserError("기존 일정 생성 위치를 확인할 수 없어 보정을 중단했습니다.")
            cursor = date.fromisoformat(config["next_date"])
            future_dates = [date.fromisoformat(row["seminar_date"]) for row in future]
            if cursor != future_dates[-1] + timedelta(weeks=1) or any(
                later - earlier != timedelta(weeks=1)
                for earlier, later in zip(future_dates, future_dates[1:], strict=False)
            ):
                raise UserError(
                    "기존 일정에 누락된 주 또는 커서 불일치가 있어 보정을 중단했습니다."
                )
            for row in future:
                on_date = date.fromisoformat(row["seminar_date"])
                gap = (cursor - on_date).days
                if (
                    gap <= 0
                    or gap % 7
                    or row["presenter_id"]
                    != old_order[(config["next_index"] - gap // 7) % len(old_order)]
                ):
                    raise UserError("기존 일정에 스왑 또는 개별 변경이 있어 보정을 중단했습니다.")
        first = next_weekday(max(today, last_date + timedelta(weeks=1)), config["weekday"])
        dates = {last_date, *(date.fromisoformat(row["seminar_date"]) for row in rows)}
        dates.update(first + timedelta(weeks=i) for i in range(12))
        start_index = rotation.presenters.index(last_presenter)
        expected = {}
        for on_date in sorted(dates):
            gap = (on_date - last_date).days
            if gap < 0 or gap % 7:
                raise UserError("기존 일정의 요일이 설정과 달라 보정을 중단했습니다.")
            expected[on_date] = rotation.presenters[
                (start_index + gap // 7) % len(rotation.presenters)
            ]
        changed = []
        for row in rows:
            if row["presenter_id"] != expected[date.fromisoformat(row["seminar_date"])]:
                if (
                    store.session_has_activity(row["id"])
                    or store.submission_receipt(row["id"]) is not None
                ):
                    raise UserError(
                        "변경할 회차에 자료 또는 알림 발송 기록이 있어 보정을 중단했습니다."
                    )
                changed.append(row["id"])
        store._revoke_submission_sessions(changed)
        store.db.executemany(
            "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?) "
            "ON CONFLICT(guild_id, seminar_date) DO UPDATE SET presenter_id=excluded.presenter_id",
            ((rotation.guild_id, str(day), presenter) for day, presenter in expected.items()),
        )
        end = max(dates) + timedelta(weeks=1)
        index = (start_index + (end - last_date).days // 7) % len(rotation.presenters)
        store.db.execute(
            "UPDATE guilds SET rotation=?, next_date=?, next_index=? WHERE guild_id=?",
            (json.dumps(rotation.presenters), str(end), index, rotation.guild_id),
        )
        store.remember_rotation_file(rotation.guild_id, rotation.fingerprint)
        unchanged()
    return first, expected[first]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="JSON을 유지하며 지난 발표일·발표자를 기준으로 DB의 진행 위치를 보정합니다."
    )
    parser.add_argument("--last-date", required=True, help="기준 발표일 YYYY-MM-DD")
    parser.add_argument("--last-presenter", required=True, help="기준 발표자의 Discord 사용자 ID")
    args = parser.parse_args()
    load_dotenv()
    last_date = parse_date(args.last_date)
    last_presenter = snowflake(args.last_presenter)
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    rotation_path = Path(os.getenv("ROTATION_FILE", "config/rotation.json")).resolve()
    database_path = data_dir / "seminar.sqlite3"
    if not database_path.is_file():
        raise UserError("기존 세미나 DB를 찾을 수 없습니다. DATA_DIR를 확인해주세요.")
    with instance_lock(data_dir):
        store = Store(database_path)
        try:
            configs = store.all_configs()
            target = (
                os.getenv("SEMINAR_GUILD_ID", "").strip()
                or os.getenv("DISCORD_GUILD_ID", "").strip()
            )
            if target:
                guild_id = snowflake(target)
            elif len(configs) == 1:
                guild_id = configs[0]["guild_id"]
            else:
                raise UserError(".env의 SEMINAR_GUILD_ID에 운영 서버를 지정해주세요.")
            config = store.require_config(guild_id)
            original = rotation_path.read_bytes()

            def unchanged() -> None:
                if rotation_path.read_bytes() != original:
                    raise UserError("작업 중 JSON이 변경되어 DB 보정을 취소했습니다.")

            rotations = read_rotations(rotation_path, default_guild_id=guild_id)
            rotation = next((entry for entry in rotations if entry.guild_id == guild_id), None)
            if rotation is None:
                raise UserError("JSON에서 운영 서버의 발표 순서를 찾을 수 없습니다.")
            unchanged()
            backup_dir = data_dir / "backups"
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup = backup_dir / (
                "before-progress-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-")
                + secrets.token_hex(4)
                + ".sqlite3"
            )
            destination = sqlite3.connect(backup)
            try:
                store.db.backup(destination)
            finally:
                destination.close()
            backup.chmod(0o600)
            print(f"DB 백업: {backup}", flush=True)
            first, presenter = align_progress(
                store, rotation, last_date, last_presenter, local_now(config).date(), unchanged
            )
            names = dict(zip(rotation.presenters, rotation.names, strict=True))
            print(f"기준 회차: {last_date} · {names.get(last_presenter) or last_presenter}")
            print(f"다음 회차: {first} · {names.get(presenter) or presenter}")
            print("완료: JSON은 그대로이며 재시작 후에도 DB의 진행 위치를 유지합니다.")
        finally:
            store.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"보정 실패: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
