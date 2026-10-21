from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .documents import DocumentInfo, normalize_search
from .submissions import classify_document, count_submission


class UserError(ValueError):
    """An actionable error safe to show in Discord."""


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise UserError("날짜는 YYYY-MM-DD 형식으로 입력해주세요.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UserError("존재하지 않는 날짜입니다.") from exc


def parse_time(value: str) -> time:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise UserError("시간은 HH:MM 형식으로 입력해주세요. 예: 09:00")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise UserError("시간은 00:00~23:59 사이여야 합니다.") from exc


def next_weekday(today: date, weekday: int) -> date:
    return today + timedelta(days=(weekday - today.weekday()) % 7)


def local_now(config: dict, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(config["timezone"]))


class Store:
    """Small, synchronous SQLite transactions; no network or parsing inside transactions."""

    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 3:
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
                timezone TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS rotation_file_state (
                guild_id INTEGER PRIMARY KEY REFERENCES guilds(guild_id),
                fingerprint TEXT NOT NULL
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(documents)")}
        with self.db:
            self.db.execute("BEGIN")
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
            self.db.execute("PRAGMA user_version=3")

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
    ) -> int:
        if weekday not in range(7):
            raise UserError("요일 값이 올바르지 않습니다.")
        if parse_time(morning) >= parse_time(evening):
            raise UserError("아침 알림 시간은 저녁 요청 시간보다 빨라야 합니다.")
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise UserError("시간대를 확인해주세요. 예: Asia/Seoul, UTC") from exc
        today = (now or datetime.now(UTC)).astimezone(zone).date()
        old = self.config(guild_id)
        delta = (weekday - old["weekday"]) % 7 if old else 0
        with self.db:
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
                    morning, evening, timezone) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id=excluded.channel_id, upload_channel_id=excluded.upload_channel_id,
                    weekday=excluded.weekday, morning=excluded.morning,
                    evening=excluded.evening, timezone=excluded.timezone
            """,
                (guild_id, channel_id, upload_channel_id, weekday, morning, evening, timezone),
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
            # If swapping today's speaker after a reminder, notify the new speaker on next tick.
            self.db.execute(
                "DELETE FROM deliveries WHERE session_id IN (?,?) AND on_date>=?",
                (one["id"], two["id"], today.isoformat()),
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
    def _search_text(title: str, filename: str, doi: str | None, arxiv_id: str | None) -> str:
        # Keep field boundaries so queries cannot accidentally span two field values.
        return "\n".join(
            normalize_search(value or "") for value in (title, filename, doi, arxiv_id)
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
    ) -> tuple[int, list[dict]]:
        config = self.require_config(guild_id)
        self._validate_kind(kind, filename)
        source_channel_id = source_channel_id or config["upload_channel_id"]
        uploaded_date = local_now(config, uploaded_at).date()
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
                    extraction_method, warning, file_path, message_url, uploaded_at, kind, classification_reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    self._search_text(title, filename, info.doi, info.arxiv_id),
                    info.text,
                    info.extraction_method,
                    info.warning,
                    file_path,
                    message_url,
                    uploaded_at.isoformat(),
                    kind,
                    classification_reason,
                ),
            )
        return cursor.lastrowid, duplicates

    def document(self, guild_id: int, record_id: int) -> dict:
        row = self.db.execute(
            """
            SELECT d.*, s.seminar_date, s.presenter_id FROM documents d
            LEFT JOIN sessions s ON s.id=d.session_id WHERE d.guild_id=? AND d.id=?
        """,
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
            """
            SELECT d.*, s.seminar_date, s.presenter_id FROM documents d
            LEFT JOIN sessions s ON s.id=d.session_id
            WHERE d.guild_id=? AND instr(d.search_text, ?)>0
        """
            + visibility
            + """
            ORDER BY d.id DESC LIMIT ? OFFSET ?
        """,
            (guild_id, term, *channel_params, page_size, (page - 1) * page_size),
        ).fetchall()
        return [dict(row) for row in rows], count

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
                "UPDATE documents SET title=?, search_text=? WHERE id=? AND guild_id=?",
                (
                    title,
                    self._search_text(title, record["filename"], record["doi"], record["arxiv_id"]),
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
                "UPDATE documents SET session_id=? WHERE guild_id=? AND id=?",
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
        self._validate_kind(kind, record["filename"])
        with self.db:
            self.db.execute(
                "UPDATE documents SET kind=?, classification_reason='사용자가 확인한 구분' WHERE guild_id=? AND id=?",
                (kind, guild_id, record_id),
            )
        return self.document(guild_id, record_id)

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
