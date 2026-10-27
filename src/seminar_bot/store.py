from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .documents import DocumentInfo, normalize_search, validate_paper_fields
from .submissions import classify_document, count_submission


class UserError(ValueError):
    """An actionable error safe to show in Discord."""


_DOCUMENT_JOINS = (
    " LEFT JOIN sessions s ON s.id=d.session_id AND s.guild_id=d.guild_id "
    "LEFT JOIN archive_presentations a ON a.id=d.archive_presentation_id AND a.guild_id=d.guild_id "
)
_DOCUMENT_COLUMNS = """
    d.*,COALESCE(s.seminar_date,a.seminar_date) AS seminar_date,
    COALESCE(s.presenter_id,a.presenter_id) AS presenter_id,
    a.presenter_name_snapshot,
    COALESCE(a.seminar_year,CAST(substr(s.seminar_date,1,4) AS INTEGER)) AS seminar_year,
    CASE WHEN a.id IS NOT NULL THEN 'archive:'||a.id
         WHEN s.id IS NOT NULL THEN 'session:'||s.id END AS record_key,
    a.archive_kind,a.label AS archive_label,(a.id IS NOT NULL) AS archived,
    CASE WHEN COALESCE(s.seminar_date,a.seminar_date) IS NOT NULL THEN 'day'
         WHEN a.id IS NOT NULL THEN 'year' ELSE 'unknown' END AS date_precision
"""


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise UserError("날짜는 YYYY-MM-DD 형식으로 입력해주세요.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UserError("존재하지 않는 날짜입니다.") from exc


def parse_time(value: str) -> time:
    if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise UserError("시간은 HH:MM 형식으로 입력해주세요. 예: 09:00")
    try:
        return time(hour=int(value[:2]), minute=int(value[3:]))
    except ValueError as exc:
        raise UserError("시간은 00:00~23:59 사이여야 합니다.") from exc


def next_weekday(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def local_now(config: dict, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(config["timezone"]))


def archive_search_term(query: str) -> str:
    """Validate optional archive search and normalize titles and paper identifiers."""
    try:
        valid = isinstance(query, str) and len(query) <= 200 and len(query.encode("utf-8")) <= 400
    except UnicodeError:
        valid = False
    if not valid or any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise UserError("검색어는 200자, 400바이트 이내로 입력해주세요.")
    raw = query.strip()
    raw = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", raw, flags=re.I)
    raw = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)", "", raw, flags=re.I)
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v\d+)?(?:\.pdf)?", raw, re.I):
        raw = re.sub(r"(?:v\d+)?(?:\.pdf)?$", "", raw, flags=re.I)
    term = normalize_search(raw)
    if query.strip() and (not term or len(term) > 200):
        raise UserError("검색할 제목 또는 식별자를 입력해주세요.")
    return term


class Store:
    """Small, synchronous SQLite transactions; no network or parsing inside transactions."""

    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 10:
            self.db.close()
            raise ValueError("데이터베이스 버전이 봇보다 새롭습니다. 봇을 업데이트해주세요.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS guilds (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                upload_channel_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                morning TEXT NOT NULL,
                evening TEXT NOT NULL,
                before_reminder TEXT,
                timezone TEXT NOT NULL,
                seminar_time TEXT,
                notifications_enabled INTEGER NOT NULL DEFAULT 0 CHECK(notifications_enabled IN (0,1)),
                reminder_start_date TEXT,
                rotation TEXT NOT NULL DEFAULT '[]',
                next_date TEXT,
                next_index INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
                seminar_date TEXT NOT NULL,
                presenter_id INTEGER NOT NULL,
                UNIQUE(guild_id, seminar_date)
            );
            CREATE TABLE IF NOT EXISTS archive_presentations (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
                presenter_id INTEGER,
                presenter_name_snapshot TEXT NOT NULL DEFAULT '',
                seminar_year INTEGER NOT NULL CHECK(seminar_year BETWEEN 1000 AND 9999),
                seminar_date TEXT,
                label TEXT NOT NULL DEFAULT '',
                archive_kind TEXT NOT NULL CHECK(archive_kind IN ('presentation','unassigned')),
                import_key TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(guild_id, import_key)
            );
            CREATE TABLE IF NOT EXISTS holidays (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
                seminar_date TEXT NOT NULL,
                UNIQUE(guild_id, seminar_date)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                on_date TEXT NOT NULL,
                kind TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(session_id, on_date, kind)
            );
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
                attachment_id INTEGER NOT NULL,
                uploader_id INTEGER NOT NULL,
                source_channel_id INTEGER NOT NULL,
                session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
                title TEXT NOT NULL,
                filename TEXT NOT NULL,
                doi TEXT,
                arxiv_id TEXT,
                sha256 TEXT NOT NULL,
                search_text TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                extraction_method TEXT NOT NULL,
                warning TEXT,
                file_path TEXT NOT NULL,
                message_url TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'unknown' CHECK(kind IN ('slides','paper','unknown')),
                classification_reason TEXT NOT NULL DEFAULT '',
                UNIQUE(guild_id, attachment_id)
            );
            CREATE INDEX IF NOT EXISTS document_guild ON documents(guild_id, id);
            CREATE INDEX IF NOT EXISTS document_session ON documents(session_id);
            CREATE TABLE IF NOT EXISTS archive_import_items (
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id),
                import_key TEXT NOT NULL,
                archive_presentation_id INTEGER NOT NULL REFERENCES archive_presentations(id),
                document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
                input_fingerprint TEXT NOT NULL,
                PRIMARY KEY(guild_id, import_key)
            );
            CREATE TABLE IF NOT EXISTS rotation_file_state (
                guild_id INTEGER PRIMARY KEY REFERENCES guilds(guild_id),
                fingerprint TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_submission_receipts (
                session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                fingerprint TEXT NOT NULL,
                message_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_upload_tombstones (
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
                upload_key TEXT NOT NULL,
                PRIMARY KEY(guild_id, upload_key)
            );
            CREATE TABLE IF NOT EXISTS submission_invitations (
                token_hash TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                presenter_id INTEGER NOT NULL,
                upload_channel_id INTEGER NOT NULL,
                session_version TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS submission_invitation_expiry
                ON submission_invitations(expires_at);
            CREATE TABLE IF NOT EXISTS submission_grants (
                token_hash TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL REFERENCES guilds(guild_id) ON DELETE CASCADE,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                presenter_id INTEGER NOT NULL,
                upload_channel_id INTEGER NOT NULL,
                session_version TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS submission_grant_expiry
                ON submission_grants(expires_at);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(documents)")}
        with self.db:
            self.db.execute("BEGIN")
            guild_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(guilds)")}
            for name, definition in (
                ("seminar_time", "TEXT"),
                (
                    "notifications_enabled",
                    "INTEGER NOT NULL DEFAULT 0 CHECK(notifications_enabled IN (0,1))",
                ),
                ("reminder_start_date", "TEXT"),
                ("before_reminder", "TEXT"),
            ):
                if name not in guild_columns:
                    self.db.execute(f"ALTER TABLE guilds ADD COLUMN {name} {definition}")
            if "kind" not in columns:
                self.db.execute(
                    "ALTER TABLE documents ADD COLUMN kind TEXT NOT NULL DEFAULT 'unknown' CHECK(kind IN ('slides','paper','unknown'))"
                )
                self.db.execute(
                    "ALTER TABLE documents ADD COLUMN classification_reason TEXT NOT NULL DEFAULT ''"
                )
                # Upgrade existing archives in place; keep IDs, files and session links.
                for record in self.db.execute("SELECT * FROM documents").fetchall():
                    info = DocumentInfo(
                        record["title"],
                        record["extracted_text"],
                        record["doi"],
                        record["arxiv_id"],
                        record["sha256"],
                        record["extraction_method"],
                        record["warning"],
                    )
                    classification = classify_document(record["filename"], info)
                    self.db.execute(
                        "UPDATE documents SET kind=?, classification_reason=? WHERE id=?",
                        (classification.kind, classification.reason, record["id"]),
                    )
            if "source_type" not in columns:
                self.db.execute(
                    "ALTER TABLE documents ADD COLUMN source_type TEXT NOT NULL DEFAULT 'discord'"
                )
                self.db.execute("ALTER TABLE documents ADD COLUMN upload_key TEXT")
            for name, definition in (
                ("venue", "TEXT"),
                ("year", "INTEGER"),
                ("link", "TEXT"),
                ("edit_revision", "INTEGER NOT NULL DEFAULT 0"),
                (
                    "archive_presentation_id",
                    "INTEGER REFERENCES archive_presentations(id) ON DELETE SET NULL",
                ),
            ):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE documents ADD COLUMN {name} {definition}")
            for table in ("submission_invitations", "submission_grants"):
                if "scope" not in {
                    row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")
                }:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN scope TEXT NOT NULL DEFAULT 'session' CHECK(scope IN ('session','presenter'))"
                    )
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS web_upload_key ON documents(guild_id, upload_key) "
                "WHERE upload_key IS NOT NULL"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS document_archive ON documents(archive_presentation_id)"
            )
            for operation in ("INSERT", "UPDATE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS archive_document_{operation.lower()} "
                    f"BEFORE {operation} ON documents "
                    "WHEN NEW.archive_presentation_id IS NOT NULL AND "
                    "(NEW.session_id IS NOT NULL OR NOT EXISTS(SELECT 1 FROM archive_presentations a "
                    "WHERE a.id=NEW.archive_presentation_id AND a.guild_id=NEW.guild_id)) "
                    "BEGIN SELECT RAISE(ABORT,'Invalid archive presentation reference'); END"
                )
            self.db.execute("PRAGMA user_version=10")

    def close(self) -> None:
        self.db.close()

    def config(self, guild_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM guilds WHERE guild_id=?", (guild_id,)).fetchone()
        return dict(row) if row else None

    def require_config(self, guild_id: int) -> dict:
        config = self.config(guild_id)
        if config is None:
            raise UserError("서버 관리자가 먼저 /설정 명령어로 채널과 시간을 설정해주세요.")
        return config

    def all_configs(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM guilds")]

    def configure(
        self,
        guild_id: int,
        channel_id: int,
        upload_channel_id: int,
        weekday: int,
        morning: str,
        evening: str,
        timezone: str,
        now: datetime | None = None,
        *,
        seminar_time: str | None = None,
        notifications_enabled: bool | None = None,
        reminder_start_date: date | None = None,
        before_reminder: str | None = None,
    ) -> int:
        if weekday not in range(7):
            raise UserError("요일 값이 올바르지 않습니다.")
        if seminar_time is not None:
            if not isinstance(seminar_time, str):
                raise UserError("세미나 시작 시간은 HH:MM 형식으로 입력해주세요.")
            parse_time(seminar_time)
        if notifications_enabled is not None and type(notifications_enabled) is not bool:
            raise UserError("알림 설정은 켜기 또는 끄기를 선택해주세요.")
        if reminder_start_date is not None and type(reminder_start_date) is not date:
            raise UserError("알림 시작일은 YYYY-MM-DD 날짜로 입력해주세요.")
        announcement_time, after_time = parse_time(morning), parse_time(evening)
        if announcement_time >= after_time:
            raise UserError("일정 안내 시간은 종료 후 요청 시간보다 빨라야 합니다.")
        old = self.config(guild_id)
        before = (
            before_reminder
            if before_reminder is not None
            else old.get("before_reminder")
            if old
            else None
        )
        if before is not None:
            if not isinstance(before, str):
                raise UserError("시작 전 알림 시간은 HH:MM 형식으로 입력해주세요.")
            if not announcement_time < parse_time(before) < after_time:
                raise UserError(
                    "알림 시간은 일정 안내 < 시작 전 요청 < 종료 후 요청 순서여야 합니다."
                )
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise UserError("시간대를 확인해주세요. 예: Asia/Seoul, UTC") from exc
        today = (now or datetime.now(UTC)).astimezone(zone).date()
        enabled = bool(old["notifications_enabled"]) if old else False
        if notifications_enabled is not None:
            enabled = notifications_enabled
        reminder_start = (
            reminder_start_date.isoformat()
            if reminder_start_date is not None
            else old["reminder_start_date"]
            if old
            else None
        )
        if enabled and not reminder_start:
            raise UserError(
                "처음 알림을 켤 때는 등록된 첫 세미나 날짜를 알림시작일로 지정해주세요."
            )
        if (
            reminder_start_date is not None
            and not self.db.execute(
                "SELECT 1 FROM sessions s WHERE s.guild_id=? AND s.seminar_date=? "
                "AND NOT EXISTS(SELECT 1 FROM holidays h WHERE h.guild_id=s.guild_id "
                "AND h.seminar_date=s.seminar_date)",
                (guild_id, reminder_start),
            ).fetchone()
        ):
            raise UserError(
                "알림 시작일에 등록된 발표 회차가 없습니다. 실제 첫 세미나 기록을 확인해주세요."
            )
        delta = (weekday - old["weekday"]) % 7 if old else 0
        with self.db:
            if old and old["upload_channel_id"] != upload_channel_id:
                for table in ("submission_invitations", "submission_grants"):
                    self.db.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
            if delta:
                self._shift_dates(guild_id, today, delta)
                # A long-stopped bot may have an ungenerated cursor in the past.
                # It must also adopt the new weekday before skipping missed weeks.
                if old["next_date"] and old["next_date"] < today.isoformat():
                    shifted = date.fromisoformat(old["next_date"]) + timedelta(days=delta)
                    self.db.execute(
                        "UPDATE guilds SET next_date=? WHERE guild_id=?",
                        (shifted.isoformat(), guild_id),
                    )
            self.db.execute(
                """
                INSERT INTO guilds(guild_id, channel_id, upload_channel_id, weekday,
                    morning, evening, timezone, seminar_time, notifications_enabled,
                    reminder_start_date, before_reminder) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id, upload_channel_id=excluded.upload_channel_id,
                    weekday=excluded.weekday, morning=excluded.morning,
                    evening=excluded.evening, timezone=excluded.timezone,
                    seminar_time=COALESCE(excluded.seminar_time, guilds.seminar_time),
                    notifications_enabled=excluded.notifications_enabled,
                    reminder_start_date=excluded.reminder_start_date,
                    before_reminder=excluded.before_reminder
            """,
                (
                    guild_id,
                    channel_id,
                    upload_channel_id,
                    weekday,
                    morning,
                    evening,
                    timezone,
                    seminar_time,
                    int(enabled),
                    reminder_start,
                    before,
                ),
            )
        return delta

    def _shift_dates(self, guild_id: int, from_date: date, days: int) -> int:
        """Caller owns transaction. Descending updates avoid UNIQUE date collisions."""
        rows = self.db.execute(
            "SELECT id, seminar_date FROM sessions WHERE guild_id=? AND seminar_date>=? "
            "ORDER BY seminar_date DESC",
            (guild_id, from_date.isoformat()),
        ).fetchall()
        for row in rows:
            self._revoke_submission_sessions([row["id"]])
            shifted = date.fromisoformat(row["seminar_date"]) + timedelta(days=days)
            self.db.execute(
                "UPDATE sessions SET seminar_date=? WHERE id=?", (shifted.isoformat(), row["id"])
            )
        # Previously skipped weeks also follow any later shift of the timetable.
        holidays = self.db.execute(
            "SELECT id, seminar_date FROM holidays WHERE guild_id=? AND seminar_date>=? "
            "ORDER BY seminar_date DESC",
            (guild_id, from_date.isoformat()),
        ).fetchall()
        for row in holidays:
            shifted = date.fromisoformat(row["seminar_date"]) + timedelta(days=days)
            self.db.execute(
                "UPDATE holidays SET seminar_date=? WHERE id=?", (shifted.isoformat(), row["id"])
            )
        config = self.require_config(guild_id)
        if config["next_date"] and config["next_date"] >= from_date.isoformat():
            shifted = date.fromisoformat(config["next_date"]) + timedelta(days=days)
            self.db.execute(
                "UPDATE guilds SET next_date=? WHERE guild_id=?", (shifted.isoformat(), guild_id)
            )
        return len(rows)

    def rotation_fingerprint(self, guild_id: int) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM rotation_file_state WHERE guild_id=?", (guild_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def session_has_activity(self, session_id: int) -> bool:
        row = self.db.execute(
            "SELECT EXISTS(SELECT 1 FROM documents WHERE session_id=?) "
            "OR EXISTS(SELECT 1 FROM deliveries WHERE session_id=?)",
            (session_id, session_id),
        ).fetchone()
        return bool(row[0])

    def remember_rotation_file(self, guild_id: int, fingerprint: str) -> None:
        # Caller owns the transaction, so the imported rotation and fingerprint
        # commit together. Unchanged files must not undo swaps or holidays.
        self.db.execute(
            "INSERT INTO rotation_file_state(guild_id, fingerprint) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET fingerprint=excluded.fingerprint",
            (guild_id, fingerprint),
        )

    def set_rotation(
        self,
        guild_id: int,
        presenters: list[int],
        start: date,
        today: date,
        *,
        fingerprint: str | None = None,
    ) -> None:
        config = self.require_config(guild_id)
        if not presenters or len(presenters) > 100 or len(set(presenters)) != len(presenters):
            raise UserError("발표자는 중복 없이 1~100명까지 등록할 수 있습니다.")
        if any(user_id <= 0 for user_id in presenters):
            raise UserError("발표자 ID가 올바르지 않습니다.")
        if start < today or start.weekday() != config["weekday"]:
            raise UserError("시작일은 오늘 이후이며 설정한 세미나 요일과 같아야 합니다.")
        with self.db:
            protected = self.db.execute(
                """
                SELECT 1 FROM sessions s WHERE s.guild_id=? AND s.seminar_date>=?
                AND (EXISTS(SELECT 1 FROM documents d WHERE d.session_id=s.id)
                  OR EXISTS(SELECT 1 FROM deliveries r WHERE r.session_id=s.id)) LIMIT 1
            """,
                (guild_id, start.isoformat()),
            ).fetchone()
            if protected:
                raise UserError(
                    "변경할 일정에 자료 또는 발송 기록이 있습니다. 그 이후 날짜부터 순서를 설정해주세요."
                )
            self._revoke_submission_sessions(
                [
                    row["id"]
                    for row in self.db.execute(
                        "SELECT id FROM sessions WHERE guild_id=? AND seminar_date>=?",
                        (guild_id, start.isoformat()),
                    )
                ]
            )
            self.db.execute(
                "DELETE FROM sessions WHERE guild_id=? AND seminar_date>=?",
                (guild_id, start.isoformat()),
            )
            self.db.execute(
                "DELETE FROM holidays WHERE guild_id=? AND seminar_date>=?",
                (guild_id, start.isoformat()),
            )
            self.db.execute(
                "UPDATE guilds SET rotation=?, next_date=?, next_index=0 WHERE guild_id=?",
                (json.dumps(presenters), start.isoformat(), guild_id),
            )
            if fingerprint is not None:
                self.remember_rotation_file(guild_id, fingerprint)
        self.ensure_horizon(guild_id, today)

    def ensure_horizon(self, guild_id: int, today: date, count: int = 12) -> None:
        config = self.require_config(guild_id)
        rotation = json.loads(config["rotation"])
        if not rotation or not config["next_date"]:
            return
        current_count = self.db.execute(
            "SELECT COUNT(*) FROM sessions WHERE guild_id=? AND seminar_date>=?",
            (guild_id, today.isoformat()),
        ).fetchone()[0]
        next_date = date.fromisoformat(config["next_date"])
        index = config["next_index"]
        # After a long downtime, advance the cycle without replaying missed reminders.
        if next_date < today:
            missed = (today - next_date).days // 7
            if next_date + timedelta(weeks=missed) < today:
                missed += 1
            next_date += timedelta(weeks=missed)
            index += missed
        with self.db:
            for _ in range(max(0, count - current_count)):
                self.db.execute(
                    "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                    (guild_id, next_date.isoformat(), rotation[index % len(rotation)]),
                )
                index += 1
                next_date += timedelta(weeks=1)
            self.db.execute(
                "UPDATE guilds SET next_date=?, next_index=? WHERE guild_id=?",
                (next_date.isoformat(), index % len(rotation), guild_id),
            )

    def upcoming(self, guild_id: int, today: date, limit: int = 8) -> list[dict]:
        self.ensure_horizon(guild_id, today, max(12, limit))
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM sessions WHERE guild_id=? AND seminar_date>=? ORDER BY seminar_date LIMIT ?",
                (guild_id, today.isoformat(), limit),
            )
        ]

    def session_on(self, guild_id: int, on_date: date) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND seminar_date=?",
            (guild_id, on_date.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    def session(self, guild_id: int, session_id: int) -> dict:
        row = self.db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND id=?", (guild_id, session_id)
        ).fetchone()
        if not row:
            raise UserError("해당 발표 회차를 찾을 수 없습니다.")
        return dict(row)

    def recent_session(self, guild_id: int, today: date) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND seminar_date BETWEEN ? AND ? ORDER BY seminar_date DESC LIMIT 1",
            (guild_id, (today - timedelta(days=7)).isoformat(), today.isoformat()),
        ).fetchone()
        return dict(row) if row else None

    def holidays(self, guild_id: int, today: date, limit: int = 20) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM holidays WHERE guild_id=? AND seminar_date>=? ORDER BY seminar_date LIMIT ?",
                (guild_id, today.isoformat(), limit),
            )
        ]

    def postpone(
        self, guild_id: int, today: date, from_date: date | None = None
    ) -> tuple[date, int]:
        upcoming = self.upcoming(guild_id, today)
        if not upcoming:
            raise UserError("먼저 서버의 config/rotation.json 파일에 발표 순서를 등록해주세요.")
        start = from_date or date.fromisoformat(upcoming[0]["seminar_date"])
        if start < today or self.session_on(guild_id, start) is None:
            raise UserError(
                "오늘 이후에 예정된 발표 날짜를 선택해주세요. /일정 에서 확인할 수 있습니다."
            )
        with self.db:
            count = self._shift_dates(guild_id, start, 7)
            self.db.execute(
                "INSERT INTO holidays(guild_id, seminar_date) VALUES(?,?)",
                (guild_id, start.isoformat()),
            )
        return start, count

    def swap(self, guild_id: int, first: date, second: date, today: date) -> None:
        if first == second or min(first, second) < today:
            raise UserError("오늘 이후의 서로 다른 두 발표 날짜를 입력해주세요.")
        one, two = self.session_on(guild_id, first), self.session_on(guild_id, second)
        if not one or not two:
            raise UserError(
                "두 날짜 모두 예정된 발표가 있어야 합니다. /일정 에서 날짜를 확인해주세요."
            )
        with self.db:
            self._revoke_submission_sessions([one["id"], two["id"]])
            self.db.execute(
                "UPDATE sessions SET presenter_id=? WHERE id=?", (two["presenter_id"], one["id"])
            )
            self.db.execute(
                "UPDATE sessions SET presenter_id=? WHERE id=?", (one["presenter_id"], two["id"])
            )
            # Preuploaded materials follow their presenter to the exchanged occurrence.
            self.db.execute(
                """
                UPDATE documents SET session_id=CASE WHEN session_id=? THEN ? ELSE ? END
                WHERE guild_id=? AND session_id IN (?,?)
            """,
                (one["id"], two["id"], one["id"], guild_id, one["id"], two["id"]),
            )
            # Preserve sent slots: swapping speakers must not replay today's
            # announcements. Any later slot reads the newly assigned speaker.

    def _revoke_submission_sessions(self, session_ids: list[int]) -> None:
        """Caller owns the schedule transaction; old links never revive after a swap back."""
        for table in ("submission_invitations", "submission_grants"):
            for value in session_ids:
                self.db.execute(
                    f"DELETE FROM {table} WHERE scope='presenter' AND guild_id IN "
                    "(SELECT guild_id FROM sessions WHERE id=?)",
                    (value,),
                )
            self.db.executemany(
                f"DELETE FROM {table} WHERE session_id=?", ((value,) for value in session_ids)
            )

    def was_sent(self, session_id: int, on_date: str, kind: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE session_id=? AND on_date=? AND kind=?",
                (session_id, on_date, kind),
            ).fetchone()
            is not None
        )

    def mark_sent(self, session_id: int, on_date: str, kind: str, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?)",
                (session_id, on_date, kind, message_id, datetime.now(UTC).isoformat()),
            )

    def has_attachment(self, guild_id: int, attachment_id: int) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM documents WHERE guild_id=? AND attachment_id=?",
                (guild_id, attachment_id),
            ).fetchone()
            is not None
        )

    def guess_session(self, guild_id: int, uploader_id: int, uploaded_date: date) -> int | None:
        candidates = self.db.execute(
            """
            SELECT id, seminar_date FROM sessions WHERE guild_id=? AND presenter_id=?
            AND seminar_date BETWEEN ? AND ? ORDER BY seminar_date
        """,
            (
                guild_id,
                uploader_id,
                (uploaded_date - timedelta(days=7)).isoformat(),
                (uploaded_date + timedelta(days=7)).isoformat(),
            ),
        ).fetchall()
        if not candidates:
            return None
        row = min(
            candidates,
            key=lambda r: (
                abs((date.fromisoformat(r["seminar_date"]) - uploaded_date).days),
                r["seminar_date"] > uploaded_date.isoformat(),
            ),
        )
        return row["id"]

    @staticmethod
    def _search_text(
        title: str,
        filename: str,
        doi: str | None,
        arxiv_id: str | None,
        venue: str | None = None,
        year: int | None = None,
        link: str | None = None,
    ) -> str:
        # Keep field boundaries so queries cannot accidentally span two field values.
        return "\n".join(
            normalize_search(str(value or ""))
            for value in (title, filename, doi, arxiv_id, venue, year, link)
        )

    def add_document(
        self,
        guild_id: int,
        attachment_id: int,
        uploader_id: int,
        info: DocumentInfo,
        filename: str,
        file_path: str,
        message_url: str,
        uploaded_at: datetime,
        source_channel_id: int | None = None,
        kind: str = "unknown",
        classification_reason: str = "",
        *,
        session_id: int | None = None,
        source_type: str = "discord",
        upload_key: str | None = None,
        venue: str | None = None,
        year: int | None = None,
        link: str | None = None,
    ) -> tuple[int, list[dict]]:
        config = self.require_config(guild_id)
        self._validate_kind(kind, filename)
        source_channel_id = source_channel_id or config["upload_channel_id"]
        uploaded_date = local_now(config, uploaded_at).date()
        if source_type not in {"discord", "web", "manual"}:
            raise UserError("자료 출처를 확인해주세요.")
        if source_type == "manual" and (kind != "paper" or file_path or info.text):
            raise UserError("직접 입력은 원본 파일이 없는 논문 정보에만 사용할 수 있습니다.")
        if source_type in {"web", "manual"}:
            if session_id is None or not upload_key:
                raise UserError("웹 제출에는 발표 회차와 업로드 ID가 필요합니다.")
            session = self.session(guild_id, session_id)
            if session["presenter_id"] != uploader_id:
                raise UserError("발표자가 바뀌었습니다. 발표 회차를 다시 선택해주세요.")
        else:
            self.ensure_horizon(guild_id, uploaded_date)
            session_id = self.guess_session(guild_id, uploader_id, uploaded_date)
        title = info.title[:500]
        normalized = normalize_search(title)
        duplicates = [
            dict(row)
            for row in self.db.execute(
                """
            SELECT d.id, d.title, d.message_url, s.seminar_date, s.presenter_id
            FROM documents d LEFT JOIN sessions s ON s.id=d.session_id
            WHERE d.guild_id=? AND d.source_channel_id=? AND (d.sha256=? OR (? IS NOT NULL AND d.doi=?)
                OR (? IS NOT NULL AND d.arxiv_id=?)
                OR (? >= 12 AND substr(d.search_text, 1, instr(d.search_text, char(10))-1)=?))
            ORDER BY d.id DESC LIMIT 3
        """,
                (
                    guild_id,
                    source_channel_id,
                    info.sha256,
                    info.doi,
                    info.doi,
                    info.arxiv_id,
                    info.arxiv_id,
                    len(normalized),
                    normalized,
                ),
            )
        ]
        with self.db:
            cursor = self.db.execute(
                """
                INSERT INTO documents(guild_id, attachment_id, uploader_id, source_channel_id, session_id,
                    title, filename, doi, arxiv_id, sha256, search_text, extracted_text,
                    extraction_method, warning, file_path, message_url, uploaded_at, kind, classification_reason,
                    source_type, upload_key, venue, year, link)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
                (
                    guild_id,
                    attachment_id,
                    uploader_id,
                    source_channel_id,
                    session_id,
                    title,
                    filename,
                    info.doi,
                    info.arxiv_id,
                    info.sha256,
                    self._search_text(title, filename, info.doi, info.arxiv_id, venue, year, link),
                    ""
                    if kind == "paper"
                    or (kind == "unknown" and filename.casefold().endswith(".pdf"))
                    else info.text,
                    info.extraction_method,
                    info.warning,
                    file_path,
                    message_url,
                    uploaded_at.isoformat(),
                    kind,
                    classification_reason,
                    source_type,
                    upload_key,
                    venue,
                    year,
                    link,
                ),
            )
        return cursor.lastrowid, duplicates

    def insert_archive_batch(
        self,
        guild_id: int,
        groups: list[dict],
        *,
        imported_at: datetime,
        commit: bool = True,
    ) -> dict:
        """Import reviewed history atomically without touching operational schedules.

        ``commit=False`` leaves a transaction open for the caller's final
        invariant checks. A pre-existing caller transaction is never committed.
        File copying and checksum verification happen before this method.
        """
        config = self.require_config(guild_id)
        if not isinstance(imported_at, datetime) or imported_at.utcoffset() is None:
            raise UserError("이관 시각에는 시간대가 필요합니다.")
        if not isinstance(groups, list) or len(groups) > 10000:
            raise UserError("이관할 과거 발표 목록을 확인해주세요.")
        today = local_now(config, imported_at).date()

        def text(value: object, limit: int, *, required: bool = False) -> str:
            if (
                not isinstance(value, str)
                or len(value) > limit
                or any(
                    ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in value
                )
                or required
                and (not value or value != value.strip())
            ):
                raise UserError("이관 정보에 올바르지 않은 문자열이 있습니다.")
            return value

        prepared = []
        group_keys, document_keys = set(), set()
        for group in groups:
            if not isinstance(group, dict):
                raise UserError("과거 발표 정보를 확인해주세요.")
            key = text(group.get("import_key"), 512, required=True)
            if key in group_keys:
                raise UserError("같은 과거 발표 이관 키가 중복되었습니다.")
            group_keys.add(key)
            year, day = group.get("seminar_year"), group.get("seminar_date")
            if type(year) is not int or not 1000 <= year <= today.year:
                raise UserError("과거 발표 연도를 확인해주세요.")
            if day is not None and (type(day) is not date or day.year != year or day > today):
                raise UserError("확인된 실제 발표 날짜 또는 날짜 미상을 지정해주세요.")
            presenter = group.get("presenter_id")
            if presenter is not None and (type(presenter) is not int or not 0 < presenter < 2**63):
                raise UserError("과거 발표자 ID를 확인해주세요.")
            archive_kind = group.get("archive_kind", "presentation")
            if archive_kind not in {"presentation", "unassigned"}:
                raise UserError("과거 발표 자료의 묶음 종류를 확인해주세요.")
            name = text(group.get("presenter_name_snapshot", ""), 100)
            label = text(group.get("label", ""), 300)
            documents = group.get("documents", [])
            if not isinstance(documents, list) or len(documents) > 10000:
                raise UserError("이관 자료 목록을 확인해주세요.")
            materials = []
            for item in documents:
                if not isinstance(item, dict) or not isinstance(item.get("info"), DocumentInfo):
                    raise UserError("분석된 이관 자료 정보를 확인해주세요.")
                material_key = text(item.get("import_key"), 512, required=True)
                if material_key in document_keys:
                    raise UserError("자료 이관 키는 회차별로 서로 달라야 합니다.")
                document_keys.add(material_key)
                kind = item.get("kind")
                filename = text(item.get("filename"), 240, required=True)
                if (
                    kind not in {"slides", "paper"}
                    or "/" in filename
                    or "\\" in filename
                    or len(filename.encode("utf-8")) > 240
                    or Path(filename).suffix.casefold() not in {".pdf", ".ppt", ".pptx"}
                ):
                    raise UserError("이관 파일의 이름 또는 구분을 확인해주세요.")
                self._validate_kind(kind, filename)
                info = item["info"]
                fields = validate_paper_fields(
                    {
                        "title": info.title,
                        "doi": info.doi,
                        "venue": item.get("venue"),
                        "year": item.get("year"),
                        "link": item.get("link"),
                    }
                )
                sha = text(info.sha256, 64).casefold()
                if sha and not re.fullmatch(r"[a-f0-9]{64}", sha) or not sha and kind == "slides":
                    raise UserError("이관 파일의 SHA-256을 확인해주세요.")
                arxiv_id = text(info.arxiv_id, 128) if info.arxiv_id is not None else None
                warning = text(info.warning, 1024) if info.warning is not None else None
                method = text(info.extraction_method, 100, required=True)
                body = info.text if kind == "slides" else ""
                if (
                    not isinstance(body, str)
                    or len(body) > 50000
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in body)
                ):
                    raise UserError("분석된 발표 자료 본문을 확인해주세요.")
                path = text(item.get("file_path", ""), 512) if kind == "slides" else ""
                if kind == "slides":
                    parts = PurePosixPath(path)
                    if (
                        not path
                        or "\\" in path
                        or parts.is_absolute()
                        or ".." in parts.parts
                        or len(parts.parts) < 2
                        or parts.parts[0] != "files"
                        or parts.suffix.casefold() != Path(filename).suffix.casefold()
                    ):
                        raise UserError("발표 자료는 NAS 자료 폴더 내부의 상대경로가 필요합니다.")
                if not sha:
                    method = "user_metadata"
                # Compare the original package input, not mutable DB fields.
                # Reapplying an unchanged package preserves later user edits;
                # changed metadata must be reviewed even if file bytes match.
                identity = {
                    "group": key,
                    "kind": kind,
                    "sha256": sha,
                    "metadata": fields,
                    "arxiv_id": arxiv_id,
                    "filename": filename,
                    "file_path": path,
                    "extraction_method": method,
                    "warning": warning,
                    "extracted_text": body,
                }
                fingerprint = hashlib.sha256(
                    json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                materials.append(
                    {
                        "key": material_key,
                        "fields": fields,
                        "sha": sha,
                        "arxiv": arxiv_id,
                        "warning": warning,
                        "method": method,
                        "body": body,
                        "path": path,
                        "filename": filename,
                        "kind": kind,
                        "fingerprint": fingerprint,
                    }
                )
            prepared.append(
                {
                    "key": key,
                    "presenter": presenter,
                    "name": name,
                    "year": year,
                    "day": day.isoformat() if day is not None else None,
                    "label": label,
                    "kind": archive_kind,
                    "documents": materials,
                }
            )

        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("SAVEPOINT archive_batch")
        result = {
            "inserted_groups": 0,
            "inserted_documents": 0,
            "skipped_documents": 0,
            "tombstoned_documents": 0,
            "groups": [],
            "documents": [],
        }
        try:
            for group in prepared:
                existing = self.db.execute(
                    "SELECT * FROM archive_presentations WHERE guild_id=? AND import_key=?",
                    (guild_id, group["key"]),
                ).fetchone()
                values = (
                    group["presenter"],
                    group["name"],
                    group["year"],
                    group["day"],
                    group["label"],
                    group["kind"],
                )
                if existing:
                    if (
                        tuple(
                            existing[field]
                            for field in (
                                "presenter_id",
                                "presenter_name_snapshot",
                                "seminar_year",
                                "seminar_date",
                                "label",
                                "archive_kind",
                            )
                        )
                        != values
                    ):
                        raise UserError("기존 과거 발표 이관 키의 날짜·발표자 정보가 다릅니다.")
                    archive_id = existing["id"]
                else:
                    archive_id = self.db.execute(
                        "INSERT INTO archive_presentations(guild_id,presenter_id,presenter_name_snapshot,"
                        "seminar_year,seminar_date,label,archive_kind,import_key,imported_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (guild_id, *values, group["key"], imported_at.isoformat()),
                    ).lastrowid
                    result["inserted_groups"] += 1
                result["groups"].append({"import_key": group["key"], "id": archive_id})
                for item in group["documents"]:
                    output = {
                        "import_key": item["key"],
                        "group_import_key": group["key"],
                        "created": False,
                    }
                    if self.web_upload_deleted(guild_id, item["key"]):
                        result["tombstoned_documents"] += 1
                        result["documents"].append({**output, "id": None, "tombstoned": True})
                        continue
                    ledger = self.db.execute(
                        "SELECT * FROM archive_import_items WHERE guild_id=? AND import_key=?",
                        (guild_id, item["key"]),
                    ).fetchone()
                    if ledger:
                        if (
                            ledger["input_fingerprint"] != item["fingerprint"]
                            or ledger["archive_presentation_id"] != archive_id
                        ):
                            raise UserError(
                                "기존 자료 이관 키에 다른 파일 또는 발표가 지정되었습니다."
                            )
                        if ledger["document_id"] is None:
                            raise UserError("삭제된 이관 자료는 자동으로 다시 등록할 수 없습니다.")
                        result["skipped_documents"] += 1
                        result["documents"].append({**output, "id": ledger["document_id"]})
                        continue
                    if self.web_upload(guild_id, item["key"]) is not None:
                        raise UserError("이관 키가 다른 제출 자료에 이미 사용되었습니다.")
                    attachment_id = -(
                        int(hashlib.sha256(item["key"].encode()).hexdigest(), 16) % (2**63 - 1) + 1
                    )
                    if self.has_attachment(guild_id, attachment_id):
                        raise UserError("기존 자료와 이관 식별자가 충돌했습니다.")
                    fields = item["fields"]
                    document_id = self.db.execute(
                        "INSERT INTO documents(guild_id,attachment_id,uploader_id,source_channel_id,"
                        "session_id,archive_presentation_id,title,filename,doi,arxiv_id,sha256,search_text,"
                        "extracted_text,extraction_method,warning,file_path,message_url,uploaded_at,kind,"
                        "classification_reason,source_type,upload_key,venue,year,link) "
                        "VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,'',?,?,'검토한 과거 자료 이관','import',?,?,?,?)",
                        (
                            guild_id,
                            attachment_id,
                            group["presenter"] or 0,
                            config["upload_channel_id"],
                            archive_id,
                            fields["title"],
                            item["filename"],
                            fields["doi"],
                            item["arxiv"],
                            item["sha"],
                            self._search_text(
                                fields["title"],
                                item["filename"],
                                fields["doi"],
                                item["arxiv"],
                                fields["venue"],
                                fields["year"],
                                fields["link"],
                            ),
                            item["body"],
                            item["method"],
                            item["warning"],
                            item["path"],
                            imported_at.isoformat(),
                            item["kind"],
                            item["key"],
                            fields["venue"],
                            fields["year"],
                            fields["link"],
                        ),
                    ).lastrowid
                    self.db.execute(
                        "INSERT INTO archive_import_items VALUES(?,?,?,?,?)",
                        (guild_id, item["key"], archive_id, document_id, item["fingerprint"]),
                    )
                    result["inserted_documents"] += 1
                    result["documents"].append({**output, "id": document_id, "created": True})
            self.db.execute("RELEASE SAVEPOINT archive_batch")
            if own_transaction and commit:
                self.db.commit()
            return result
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT archive_batch")
            self.db.execute("RELEASE SAVEPOINT archive_batch")
            if own_transaction:
                self.db.rollback()
            raise

    def web_upload(self, guild_id: int, upload_key: str) -> dict | None:
        row = self.db.execute(
            "SELECT id FROM documents WHERE guild_id=? AND upload_key=?", (guild_id, upload_key)
        ).fetchone()
        return self.document(guild_id, row["id"]) if row else None

    def web_upload_deleted(self, guild_id: int, upload_key: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM web_upload_tombstones WHERE guild_id=? AND upload_key=?",
                (guild_id, upload_key),
            ).fetchone()
            is not None
        )

    def submission_receipt(self, session_id: int) -> str | None:
        row = self.db.execute(
            "SELECT fingerprint FROM web_submission_receipts WHERE session_id=?", (session_id,)
        ).fetchone()
        return row["fingerprint"] if row else None

    def mark_submission_receipt(self, session_id: int, fingerprint: str, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO web_submission_receipts VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, message_id=excluded.message_id",
                (session_id, fingerprint, message_id),
            )

    def document(self, guild_id: int, record_id: int) -> dict:
        row = self.db.execute(
            "SELECT "
            + _DOCUMENT_COLUMNS
            + " FROM documents d"
            + _DOCUMENT_JOINS
            + "WHERE d.guild_id=? AND d.id=?",
            (guild_id, record_id),
        ).fetchone()
        if not row:
            raise UserError("이 서버에서 해당 자료를 찾을 수 없습니다.")
        return dict(row)

    def search(
        self,
        guild_id: int,
        query: str,
        page: int = 1,
        page_size: int = 5,
        visible_channel_ids: list[int] | None = None,
        kind: str | None = None,
    ) -> tuple[list[dict], int]:
        if page < 1:
            raise UserError("페이지는 1 이상이어야 합니다.")
        # Identifier URLs, labels and arXiv revision suffixes should work as queries too.
        raw = query.strip()
        raw = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", raw, flags=re.I)
        raw = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)", "", raw, flags=re.I)
        if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v\d+)?(?:\.pdf)?", raw, re.I):
            raw = re.sub(r"(?:v\d+)?(?:\.pdf)?$", "", raw, flags=re.I)
        term = normalize_search(raw)
        if not term or len(term) > 200:
            raise UserError("검색어는 1~200자 사이로 입력해주세요.")
        visibility = ""
        channel_params = []
        if visible_channel_ids is not None:
            if not visible_channel_ids:
                return [], 0
            channel_params = list(visible_channel_ids)
            visibility = (
                " AND d.source_channel_id IN (" + ",".join("?" for _ in channel_params) + ")"
            )
        if kind is not None:
            if kind not in {"slides", "paper", "unknown"}:
                raise UserError("자료 구분을 확인해주세요.")
            visibility += " AND d.kind=?"
            channel_params.append(kind)
        # instr binds user input literally; percent, quotes and SQL syntax are not patterns.
        count = self.db.execute(
            "SELECT COUNT(*) FROM documents d WHERE d.guild_id=? AND instr(d.search_text, ?)>0"
            + visibility,
            (guild_id, term, *channel_params),
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT "
            + _DOCUMENT_COLUMNS
            + " FROM documents d"
            + _DOCUMENT_JOINS
            + "WHERE d.guild_id=? AND instr(d.search_text, ?)>0"
            + visibility
            + """
            ORDER BY d.id DESC LIMIT ? OFFSET ?
        """,
            (guild_id, term, *channel_params, page_size, (page - 1) * page_size),
        ).fetchall()
        return [dict(row) for row in rows], count

    def archive(
        self,
        guild_id: int,
        q: str = "",
        kind: str | None = None,
        page: int = 1,
        page_size: int = 20,
        visible_channel_ids: list[int] | None = None,
    ) -> tuple[list[dict], int]:
        """Read existing materials in seminar order without creating schedule rows."""
        if type(page) is not int or not 1 <= page <= 100000:
            raise UserError("페이지는 1~100000 사이로 입력해주세요.")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise UserError("자료 표시 개수를 확인해주세요.")
        if kind is not None and kind not in {"slides", "paper"}:
            raise UserError("발표 자료 또는 논문을 선택해주세요.")
        term = archive_search_term(q)
        conditions = ["d.guild_id=?"]
        parameters: list = [guild_id]
        if visible_channel_ids is not None:
            if not visible_channel_ids:
                return [], 0
            conditions.append(
                "d.source_channel_id IN (" + ",".join("?" for _ in visible_channel_ids) + ")"
            )
            parameters.extend(visible_channel_ids)
        if kind is not None:
            conditions.append("d.kind=?")
            parameters.append(kind)
        if term:
            conditions.append("instr(d.search_text, ?)>0")
            parameters.append(term)
        where = " AND ".join(conditions)
        count = self.db.execute(
            "SELECT COUNT(*) FROM documents d WHERE " + where, parameters
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT "
            + _DOCUMENT_COLUMNS
            + " FROM documents d"
            + _DOCUMENT_JOINS
            + "WHERE "
            + where
            + " ORDER BY COALESCE(a.seminar_year,CAST(substr(s.seminar_date,1,4) AS INTEGER),"
            "CAST(substr(d.uploaded_at,1,4) AS INTEGER)) DESC,"
            "CASE WHEN a.id IS NOT NULL THEN a.seminar_date IS NOT NULL ELSE 1 END DESC,"
            "COALESCE(s.seminar_date,a.seminar_date,CASE WHEN a.id IS NULL THEN substr(d.uploaded_at,1,10) END) DESC,"
            "CASE WHEN a.id IS NULL THEN d.uploaded_at END DESC,"
            "COALESCE(a.id,s.id) DESC,d.id DESC LIMIT ? OFFSET ?",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
        return [dict(row) for row in rows], count

    def presentations(
        self,
        guild_id: int,
        through: date,
        q: str = "",
        page: int = 1,
        page_size: int = 10,
        visible_channel_ids: list[int] | None = None,
        presenter_ids: list[int] | None = None,
    ) -> tuple[list[dict], int]:
        """Page existing past presentations, keeping each session's materials together."""
        if type(page) is not int or not 1 <= page <= 100000:
            raise UserError("페이지는 1~100000 사이로 입력해주세요.")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise UserError("발표 표시 개수를 확인해주세요.")
        if type(through) is not date:
            raise UserError("조회 날짜를 확인해주세요.")
        term = archive_search_term(q)
        document_conditions = [
            "d.guild_id=p.guild_id",
            "((p.record_type='session' AND d.session_id=p.id) OR "
            "(p.record_type='archive' AND d.archive_presentation_id=p.id))",
        ]
        document_parameters: list = []
        if visible_channel_ids is not None:
            if not visible_channel_ids:
                return [], 0
            document_conditions.append(
                "d.source_channel_id IN (" + ",".join("?" for _ in visible_channel_ids) + ")"
            )
            document_parameters.extend(visible_channel_ids)
        document_where = " AND ".join(document_conditions)
        matching_where = document_where
        matching_parameters = list(document_parameters)
        if term:
            match_conditions = ["instr(d.search_text, ?)>0"]
            matching_parameters.append(term)
            if presenter_ids:
                match_conditions.append(
                    "p.presenter_id IN (" + ",".join("?" for _ in presenter_ids) + ")"
                )
                matching_parameters.extend(presenter_ids)
            named_archives = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id,presenter_name_snapshot,label,seminar_year FROM archive_presentations WHERE guild_id=?",
                    (guild_id,),
                )
                if any(
                    term in normalize_search(str(row[field]))
                    for field in ("presenter_name_snapshot", "label", "seminar_year")
                )
            ]
            if named_archives:
                match_conditions.append(
                    "(p.record_type='archive' AND p.id IN ("
                    + ",".join("?" for _ in named_archives)
                    + "))"
                )
                matching_parameters.extend(named_archives)
            matching_where += " AND (" + " OR ".join(match_conditions) + ")"
        catalog = """
            SELECT id,guild_id,presenter_id,'' AS presenter_name_snapshot,
                   CAST(substr(seminar_date,1,4) AS INTEGER) AS seminar_year,seminar_date,
                   'session' AS record_type,'session:'||id AS record_key,
                   'day' AS date_precision,0 AS archived,NULL AS archive_kind,NULL AS archive_label
            FROM sessions
            UNION ALL
            SELECT id,guild_id,presenter_id,presenter_name_snapshot,seminar_year,seminar_date,
                   'archive','archive:'||id,
                   CASE WHEN seminar_date IS NULL THEN 'year' ELSE 'day' END,1,archive_kind,label
            FROM archive_presentations
        """
        where = (
            "p.guild_id=? AND p.seminar_year<=? AND (p.seminar_date IS NULL OR p.seminar_date<=?) AND EXISTS ("
            "SELECT 1 FROM documents d WHERE " + matching_where + ")"
        )
        parameters = [guild_id, through.year, through.isoformat(), *matching_parameters]
        total = self.db.execute(
            "SELECT COUNT(*) FROM (" + catalog + ") p WHERE " + where, parameters
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT p.* FROM ("
            + catalog
            + ") p WHERE "
            + where
            + " ORDER BY p.seminar_year DESC,(p.seminar_date IS NOT NULL) DESC,"
            "p.seminar_date DESC,p.record_type DESC,p.id DESC LIMIT ? OFFSET ?",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
        if not rows:
            return [], total
        session_ids = [row["id"] for row in rows if row["record_type"] == "session"]
        archive_ids = [row["id"] for row in rows if row["record_type"] == "archive"]
        selections, selected_parameters = [], []
        for field, ids in (("session_id", session_ids), ("archive_presentation_id", archive_ids)):
            if ids:
                selections.append("d." + field + " IN (" + ",".join("?" for _ in ids) + ")")
                selected_parameters.extend(ids)
        visible = ""
        visible_parameters = []
        if visible_channel_ids is not None:
            visible = (
                " AND d.source_channel_id IN (" + ",".join("?" for _ in visible_channel_ids) + ")"
            )
            visible_parameters.extend(visible_channel_ids)
        documents = self.db.execute(
            "SELECT "
            + _DOCUMENT_COLUMNS
            + " FROM documents d"
            + _DOCUMENT_JOINS
            + "WHERE d.guild_id=? AND ("
            + " OR ".join(selections)
            + ")"
            + visible
            + " ORDER BY d.uploaded_at DESC,d.id DESC",
            (guild_id, *selected_parameters, *visible_parameters),
        ).fetchall()
        by_session = {row["record_key"]: [] for row in rows}
        for document in documents:
            if document["record_key"] in by_session:
                by_session[document["record_key"]].append(dict(document))
        return [
            {
                "session": {**dict(row), "archived": bool(row["archived"])},
                "documents": by_session[row["record_key"]],
            }
            for row in rows
        ], total

    def rename_document(
        self, guild_id: int, record_id: int, title: str, user_id: int, manager: bool
    ) -> None:
        record = self.document(guild_id, record_id)
        self._can_edit(record, user_id, manager)
        title = " ".join(title.split())
        if not 1 <= len(title) <= 500:
            raise UserError("제목은 1~500자 사이로 입력해주세요.")
        with self.db:
            self.db.execute(
                "UPDATE documents SET title=?, search_text=?, edit_revision=edit_revision+1, "
                "extraction_method=CASE WHEN kind='paper' THEN 'user_metadata' ELSE extraction_method END WHERE id=? AND guild_id=?",
                (
                    title,
                    self._search_text(
                        title,
                        record["filename"],
                        record["doi"],
                        record["arxiv_id"],
                        record["venue"],
                        record["year"],
                        record["link"],
                    ),
                    record_id,
                    guild_id,
                ),
            )

    @staticmethod
    def _can_edit(record: dict, user_id: int, manager: bool) -> None:
        if not manager and record["uploader_id"] != user_id:
            raise UserError("자료를 올린 사람 또는 서버 관리자만 수정할 수 있습니다.")

    def link_document(
        self, guild_id: int, record_id: int, seminar_date: date, user_id: int, manager: bool
    ) -> None:
        record = self.document(guild_id, record_id)
        self._can_edit(record, user_id, manager)
        session = self.session_on(guild_id, seminar_date)
        if not session:
            raise UserError("해당 날짜에 등록된 발표가 없습니다.")
        if not manager and session["presenter_id"] != user_id:
            raise UserError(
                "본인의 발표 회차에 연결해주세요. 다른 발표자 자료는 서버 관리자가 연결할 수 있습니다."
            )
        with self.db:
            self.db.execute(
                "UPDATE documents SET session_id=?, edit_revision=edit_revision+1 WHERE guild_id=? AND id=?",
                (session["id"], guild_id, record_id),
            )

    @staticmethod
    def _validate_kind(kind: str, filename: str) -> None:
        extension = Path(filename).suffix.casefold()
        if kind not in {"slides", "paper", "unknown"}:
            raise UserError("발표 자료 또는 논문으로 구분해주세요.")
        if kind == "paper" and extension != ".pdf":
            raise UserError(
                "논문은 PDF 파일만 등록할 수 있습니다. PPT/PPTX는 발표 자료로 분류해주세요."
            )

    def classify(
        self, guild_id: int, record_id: int, kind: str, user_id: int, manager: bool
    ) -> dict:
        record = self.document(guild_id, record_id)
        self._can_edit(record, user_id, manager)
        if record["source_type"] == "manual" and kind != "paper":
            raise UserError("직접 입력한 논문 정보는 발표 자료로 분류할 수 없습니다.")
        self._validate_kind(kind, record["filename"])
        with self.db:
            self.db.execute(
                "UPDATE documents SET kind=?, classification_reason='사용자가 확인한 구분', "
                "file_path=CASE WHEN ?='paper' THEN '' ELSE file_path END, "
                "extracted_text=CASE WHEN ?='paper' THEN '' ELSE extracted_text END, edit_revision=edit_revision+1 "
                "WHERE guild_id=? AND id=?",
                (kind, kind, kind, guild_id, record_id),
            )
        return self.document(guild_id, record_id)

    def update_document_metadata(self, guild_id: int, record_id: int, fields: dict) -> dict:
        record = self.document(guild_id, record_id)
        with self.db:
            if record["kind"] == "slides":
                self.db.execute(
                    "UPDATE documents SET title=?, search_text=?, edit_revision=edit_revision+1 WHERE guild_id=? AND id=?",
                    (
                        fields["title"],
                        self._search_text(
                            fields["title"],
                            record["filename"],
                            record["doi"],
                            record["arxiv_id"],
                            record["venue"],
                            record["year"],
                            record["link"],
                        ),
                        guild_id,
                        record_id,
                    ),
                )
            else:
                self.db.execute(
                    "UPDATE documents SET title=?,venue=?,year=?,doi=?,link=?,search_text=?, "
                    "extraction_method='user_metadata',edit_revision=edit_revision+1 WHERE guild_id=? AND id=?",
                    (
                        fields["title"],
                        fields["venue"],
                        fields["year"],
                        fields["doi"],
                        fields["link"],
                        self._search_text(
                            fields["title"],
                            record["filename"],
                            fields["doi"],
                            record["arxiv_id"],
                            fields["venue"],
                            fields["year"],
                            fields["link"],
                        ),
                        guild_id,
                        record_id,
                    ),
                )
            self.db.execute(
                "DELETE FROM web_submission_receipts WHERE session_id=?", (record["session_id"],)
            )
        return self.document(guild_id, record_id)

    def delete_document(self, guild_id: int, record_id: int) -> None:
        record = self.document(guild_id, record_id)
        with self.db:
            if record["upload_key"]:
                self.db.execute(
                    "INSERT OR IGNORE INTO web_upload_tombstones VALUES(?,?)",
                    (guild_id, record["upload_key"]),
                )
            self.db.execute(
                "DELETE FROM documents WHERE guild_id=? AND id=?", (guild_id, record_id)
            )
            self.db.execute(
                "DELETE FROM web_submission_receipts WHERE session_id=?", (record["session_id"],)
            )

    def other_documents_use_file(self, record_id: int, file_path: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM documents WHERE file_path=? AND id<>? LIMIT 1",
                (file_path, record_id),
            ).fetchone()
            is not None
        )

    def submission(
        self, guild_id: int, session_id: int, visible_channel_ids: list[int] | None = None
    ) -> dict:
        session = self.session(guild_id, session_id)
        rows = [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM documents WHERE guild_id=? AND session_id=? ORDER BY id",
                (guild_id, session_id),
            )
        ]
        if visible_channel_ids is not None:
            visible = set(visible_channel_ids)
            rows = [row for row in rows if row["source_channel_id"] in visible]
        return {**session, **count_submission(rows), "documents": rows}
