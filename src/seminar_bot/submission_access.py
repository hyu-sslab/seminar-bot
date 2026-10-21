"""Short-lived submission capabilities. SQLite only contains SHA-256 token hashes."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

from .store import Store, UserError

INVITATION_TTL = 15 * 60
GRANT_TTL = 60 * 60
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class SubmissionAccessDenied(ValueError):
    def __init__(self):
        super().__init__(
            "제출 링크가 만료되었거나 사용할 수 없습니다. Discord에서 /제출을 다시 실행해주세요."
        )


def session_version(row: dict) -> str:
    return hashlib.sha256(
        f"{row['id']}:{row['seminar_date']}:{row['presenter_id']}".encode()
    ).hexdigest()


def submission_link(page_url: str, token: str) -> str:
    parsed = urlsplit(page_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UserError("웹 자료 제출 주소가 설정되지 않았습니다. 서버 관리자에게 문의해주세요.")
    # Fragments are not sent to the HTTP server or included in referrers.
    return urlunsplit((parsed.scheme, parsed.netloc, "/submit/", "", "token=" + token))


class SubmissionAccess:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _hash(token: str) -> str:
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise SubmissionAccessDenied()
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _prune(self, now: int) -> None:
        for table in ("submission_invitations", "submission_grants"):
            self.store.db.execute(f"DELETE FROM {table} WHERE expires_at<=?", (now,))

    def _scope(self, grant: dict, guild_id: int, now: int) -> dict:
        if grant["guild_id"] != guild_id or grant["expires_at"] <= now:
            raise SubmissionAccessDenied()
        try:
            current = self.store.session(guild_id, grant["session_id"])
            config = self.store.require_config(guild_id)
        except UserError as exc:
            raise SubmissionAccessDenied() from exc
        if (
            current["presenter_id"] != grant["presenter_id"]
            or session_version(current) != grant["session_version"]
            or config["upload_channel_id"] != grant["upload_channel_id"]
        ):
            raise SubmissionAccessDenied()
        return grant

    def select_session(
        self, guild_id: int, presenter_id: int, today: date, on_date: date | None = None
    ) -> dict:
        config = self.store.require_config(guild_id)
        # Generate one full rotation, without adding years of unused sessions for a small lab.
        self.store.ensure_horizon(guild_id, today, max(12, len(json.loads(config["rotation"]))))
        if on_date is not None:
            row = self.store.session_on(guild_id, on_date)
            if not row or row["presenter_id"] != presenter_id:
                raise UserError(
                    "본인이 발표하는 날짜만 선택할 수 있습니다. /일정에서 확인해주세요."
                )
            return row
        recent = self.store.db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND presenter_id=? "
            "AND seminar_date BETWEEN ? AND ? ORDER BY seminar_date DESC",
            (guild_id, presenter_id, (today - timedelta(days=28)).isoformat(), today.isoformat()),
        ).fetchall()
        for row in recent:
            if not self.store.submission(guild_id, row["id"], [config["upload_channel_id"]])[
                "complete"
            ]:
                return dict(row)
        row = self.store.db.execute(
            "SELECT * FROM sessions WHERE guild_id=? AND presenter_id=? "
            "AND seminar_date>=? ORDER BY seminar_date LIMIT 1",
            (guild_id, presenter_id, today.isoformat()),
        ).fetchone()
        if not row:
            raise UserError(
                "본인의 발표 일정이 없습니다. 서버 관리자에게 발표 순서를 확인해주세요."
            )
        return dict(row)

    def issue(
        self, guild_id: int, session_id: int, presenter_id: int, *, now: int | None = None
    ) -> tuple[str, int]:
        stamp = int(time.time()) if now is None else now
        row = self.store.session(guild_id, session_id)
        config = self.store.require_config(guild_id)
        if row["presenter_id"] != presenter_id:
            raise UserError("본인의 발표 회차에만 제출 링크를 만들 수 있습니다.")
        token = secrets.token_urlsafe(32)
        expires_at = stamp + INVITATION_TTL
        with self.store.db:
            self._prune(stamp)
            for table in ("submission_invitations", "submission_grants"):
                self.store.db.execute(
                    f"DELETE FROM {table} WHERE guild_id=? AND presenter_id=? AND session_id=?",
                    (guild_id, presenter_id, session_id),
                )
            self.store.db.execute(
                "INSERT INTO submission_invitations VALUES(?,?,?,?,?,?,?)",
                (
                    self._hash(token),
                    guild_id,
                    session_id,
                    presenter_id,
                    config["upload_channel_id"],
                    session_version(row),
                    expires_at,
                ),
            )
        return token, expires_at

    def redeem(self, token: str, guild_id: int, *, now: int | None = None) -> tuple[str, int]:
        stamp = int(time.time()) if now is None else now
        digest = self._hash(token)
        # Write lock before the read ensures concurrent NAS API calls have one winner.
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self._prune(stamp)
            row = self.store.db.execute(
                "SELECT * FROM submission_invitations WHERE token_hash=?", (digest,)
            ).fetchone()
            if row is None:
                raise SubmissionAccessDenied()
            grant = self._scope(dict(row), guild_id, stamp)
            self.store.db.execute(
                "DELETE FROM submission_invitations WHERE token_hash=?", (digest,)
            )
            access_token = secrets.token_urlsafe(32)
            expires_at = stamp + GRANT_TTL
            self.store.db.execute(
                "INSERT INTO submission_grants VALUES(?,?,?,?,?,?,?)",
                (
                    self._hash(access_token),
                    guild_id,
                    grant["session_id"],
                    grant["presenter_id"],
                    grant["upload_channel_id"],
                    grant["session_version"],
                    expires_at,
                ),
            )
        return access_token, expires_at

    def validate(self, token: str, guild_id: int, *, now: int | None = None) -> dict:
        stamp = int(time.time()) if now is None else now
        row = self.store.db.execute(
            "SELECT * FROM submission_grants WHERE token_hash=?", (self._hash(token),)
        ).fetchone()
        if row is None:
            raise SubmissionAccessDenied()
        return self._scope(dict(row), guild_id, stamp)
