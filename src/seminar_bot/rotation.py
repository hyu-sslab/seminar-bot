"""Read a host-edited presenter list; export an existing schedule for migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .store import Store, UserError, local_now, next_weekday, parse_date, parse_time

MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class Rotation:
    guild_id: int
    presenters: tuple[int, ...]
    start_date: date | None
    names: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        # Display names and whitespace do not constitute an order change.
        value = json.dumps([self.guild_id, self.presenters, str(self.start_date)])
        return hashlib.sha256(value.encode()).hexdigest()


def snowflake(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
        raise UserError("서버 ID와 user_id는 숫자로 된 문자열이어야 합니다.")
    number = int(value)
    if not 0 < number <= 2**63 - 1:
        raise UserError("서버 ID 또는 user_id의 범위를 확인해주세요.")
    return number


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise UserError("JSON에 같은 키가 두 번 있습니다.")
        result[key] = value
    return result


def read_rotations(path: Path) -> list[Rotation]:
    try:
        with path.open("rb") as stream:
            content = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        raise UserError("config/rotation.json이 없습니다. 발표 순서 파일을 만들어주세요.") from None
    if len(content) > MAX_BYTES:
        raise UserError("발표 순서 JSON은 256 KiB 이하여야 합니다.")
    try:
        data = json.loads(content.decode("utf-8"), object_pairs_hook=unique_object)
    except UserError:
        raise
    except (ValueError, RecursionError) as exc:
        raise UserError(
            "발표 순서 JSON 형식이 잘못되었습니다. 괄호·쉼표·따옴표를 확인해주세요."
        ) from exc
    if not isinstance(data, dict) or set(data) != {"guilds"}:
        raise UserError('발표 순서 JSON의 최상위 항목은 "guilds"여야 합니다.')
    guilds = data["guilds"]
    if not isinstance(guilds, dict) or len(guilds) > 100:
        raise UserError('"guilds"는 서버 ID를 키로 갖는 객체여야 합니다 (최대 100개).')
    rotations = []
    seen = set()
    for guild, entry in guilds.items():
        guild_id = snowflake(guild)
        if guild_id in seen:
            raise UserError("같은 서버 ID가 중복되었습니다.")
        seen.add(guild_id)
        if (
            not isinstance(entry, dict)
            or "presenters" not in entry
            or set(entry) - {"start_date", "presenters"}
        ):
            raise UserError('서버별 항목에는 "presenters"와 선택 항목 "start_date"만 사용하세요.')
        start = entry.get("start_date")
        if start is not None:
            if not isinstance(start, str):
                raise UserError('"start_date"는 YYYY-MM-DD 문자열 또는 null이어야 합니다.')
            start = parse_date(start)
        members = entry["presenters"]
        if not isinstance(members, list) or not 1 <= len(members) <= 100:
            raise UserError('"presenters"에는 발표자를 1~100명 등록해주세요.')
        ids = []
        for member in members:
            if (
                not isinstance(member, dict)
                or "user_id" not in member
                or set(member) - {"user_id", "name"}
                or not isinstance(member.get("name", ""), str)
                or len(member.get("name", "")) > 100
            ):
                raise UserError('발표자는 "user_id"와 선택 항목 "name"으로 작성해주세요.')
            ids.append(snowflake(member["user_id"]))
        if len(set(ids)) != len(ids):
            raise UserError("같은 발표자가 중복되었습니다.")
        rotations.append(
            Rotation(guild_id, tuple(ids), start, tuple(m.get("name", "") for m in members))
        )
    return rotations


def apply_rotation(store: Store, rotation: Rotation, now: datetime) -> date | None:
    if store.rotation_fingerprint(rotation.guild_id) == rotation.fingerprint:
        return None
    config = store.require_config(rotation.guild_id)
    current = local_now(config, now)
    today = current.date()
    start = rotation.start_date
    if start is None:
        earliest = today
        # Do not replace today's presenter after either reminder or an upload.
        session = store.session_on(rotation.guild_id, today)
        if current.time() >= parse_time(config["evening"]) or (
            session and store.session_has_activity(session["id"])
        ):
            earliest += timedelta(days=1)
        upcoming = store.upcoming(rotation.guild_id, earliest, 1)
        start = (
            date.fromisoformat(upcoming[0]["seminar_date"])
            if upcoming
            else next_weekday(earliest, config["weekday"])
        )
    store.set_rotation(
        rotation.guild_id,
        list(rotation.presenters),
        start,
        today,
        fingerprint=rotation.fingerprint,
    )
    return start


def export_rotations(store: Store, now: datetime) -> dict:
    """Adopt current DB state without rebuilding its individual occurrences."""
    guilds = {}
    for config in store.all_configs():
        ids = json.loads(config["rotation"])
        if not ids:
            continue
        rows = store.upcoming(config["guild_id"], local_now(config, now).date(), 1)
        first = rows[0]["presenter_id"] if rows else ids[config["next_index"] % len(ids)]
        index = ids.index(first) if first in ids else 0
        ids = ids[index:] + ids[:index]
        guilds[str(config["guild_id"])] = {
            "start_date": None,
            "presenters": [{"user_id": str(user_id), "name": ""} for user_id in ids],
        }
        rotation = Rotation(config["guild_id"], tuple(ids), None)
        with store.db:
            store.remember_rotation_file(config["guild_id"], rotation.fingerprint)
    return {"guilds": guilds}


def main() -> None:
    # Redirect stdout into the host's editable config/rotation.json. The config
    # directory is mounted read-only; the bot cannot overwrite manual edits.
    from .__main__ import instance_lock

    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "./data")).resolve()
    try:
        with instance_lock(data_dir):
            store = Store(data_dir / "seminar.sqlite3")
            try:
                data = export_rotations(store, datetime.now(UTC))
                print(json.dumps(data, ensure_ascii=False, indent=2))
            finally:
                store.close()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
