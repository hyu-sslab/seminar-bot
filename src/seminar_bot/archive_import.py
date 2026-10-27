"""Offline, explicit import of reviewed historical presentation metadata.

The default command only reads an existing database. Packages contain a JSON
manifest and presentation assets; paper originals and extracted text are never
accepted. No Discord credentials, network access, or live schedules are needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath

from .documents import DocumentInfo, validate_paper_fields

MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_MEMBERS = 2_048
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,511}")
_SHA = re.compile(r"[a-f0-9]{64}")
_APPEND_TABLES = {"documents", "archive_presentations", "archive_import_items"}


def _text(value: object, maximum: int, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not empty
        and not value.strip()
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        )
    ):
        raise ValueError("패키지의 텍스트 항목을 확인해주세요.")
    return value


def _key(value: object) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("패키지의 고정 식별자를 확인해주세요.")
    return value


def _identifier(value: object, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError("서버 또는 발표자 ID를 확인해주세요.")
    return value


def _member_name(value: object) -> str:
    value = _text(value, 240)
    path = PurePosixPath(value)
    if (
        "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("패키지 내부 파일 경로가 올바르지 않습니다.")
    return value


class ImportPackage:
    """Read a bounded ZIP or a prepared directory, without extracting paths."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.archive: zipfile.ZipFile | None = None
        if self.path.is_symlink():
            raise ValueError("패키지 심볼릭 링크는 지원하지 않습니다.")
        if self.path.is_dir():
            self.root = self.path.resolve(strict=True)
            members = {}
            for member in self.root.rglob("*"):
                if member.is_symlink():
                    raise ValueError("패키지 심볼릭 링크는 지원하지 않습니다.")
                if member.is_file():
                    members[_member_name(member.relative_to(self.root).as_posix())] = (
                        member.stat().st_size
                    )
            self.members = members
        else:
            self.archive = zipfile.ZipFile(self.path)
            self.root = None
            members = self.archive.infolist()
            if any(
                stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1
                for member in members
            ):
                self.close()
                raise ValueError("암호화되거나 링크가 포함된 패키지는 지원하지 않습니다.")
            self.members = {
                _member_name(member.filename): member.file_size
                for member in members
                if not member.is_dir()
            }
            if len({member.filename for member in members}) != len(members):
                self.close()
                raise ValueError("중복된 패키지 파일명이 있습니다.")
        if (
            len(self.members) > MAX_PACKAGE_MEMBERS
            or sum(self.members.values()) > MAX_PACKAGE_BYTES
        ):
            self.close()
            raise ValueError("패키지 용량 또는 파일 수 제한을 초과했습니다.")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        if self.archive:
            self.archive.close()

    @contextmanager
    def open(self, name: str):
        name = _member_name(name)
        if name not in self.members:
            raise ValueError("패키지에서 필요한 파일을 찾지 못했습니다.")
        if self.archive:
            with self.archive.open(name) as stream:
                yield stream
        else:
            path = self.root / name
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.root):
                raise ValueError("패키지 파일 경로가 변경되었습니다.")
            with path.open("rb") as stream:
                yield stream

    def manifest(self) -> dict:
        if self.members.get("manifest.json", MAX_MANIFEST_BYTES + 1) > MAX_MANIFEST_BYTES:
            raise ValueError("이관 manifest 크기를 확인해주세요.")
        with self.open("manifest.json") as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError("이관 manifest 크기를 확인해주세요.")
        try:
            result = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ValueError("이관 manifest JSON을 확인해주세요.") from exc
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise ValueError("지원하지 않는 이관 manifest 형식입니다.")
        return result

    def copy_verified(self, asset: str, expected: str, destination: Path | None = None) -> int:
        if (
            not _SHA.fullmatch(expected)
            or self.members.get(asset, MAX_ASSET_BYTES + 1) > MAX_ASSET_BYTES
        ):
            raise ValueError("발표 자료 해시 또는 크기를 확인해주세요.")
        digest, size = hashlib.sha256(), 0
        with self.open(asset) as source:
            target = destination.open("xb") if destination else None
            try:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_ASSET_BYTES:
                        raise ValueError("발표 자료 크기 제한을 초과했습니다.")
                    digest.update(chunk)
                    if target:
                        target.write(chunk)
            finally:
                if target:
                    target.close()
        if not size or digest.hexdigest() != expected:
            raise ValueError("발표 자료 SHA-256 검증에 실패했습니다.")
        if destination:
            destination.chmod(0o600)
        return size


@contextmanager
def _readonly(database: Path):
    if not database.is_file():
        raise ValueError("기존 세미나 데이터베이스 경로를 지정해주세요.")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.close()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_hash(values) -> str:
    return hashlib.sha256(
        json.dumps(
            list(values),
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: {"bytes": value.hex()},
        ).encode()
    ).hexdigest()


def runtime_snapshot(connection: sqlite3.Connection) -> dict:
    """Keep only row hashes; configuration and grant values never enter reports."""
    snapshot = {}
    for table in sorted(_tables(connection)):
        columns = list(connection.execute(f"PRAGMA table_info({_quote(table)})"))
        names = [row[1] for row in columns]
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        indices = [names.index(name) for name in primary]
        rows = {}
        for row in connection.execute(
            f"SELECT {','.join(map(_quote, names))} FROM {_quote(table)}"
        ):
            identity = _row_hash(row[index] for index in indices) if primary else _row_hash(row)
            record = rows.setdefault(identity, [_row_hash(row), 0])
            record[1] += 1
        snapshot[table] = {"columns": names, "primary": primary, "rows": rows}
    return snapshot


def verify_runtime_snapshot(connection: sqlite3.Connection, snapshot: dict) -> None:
    for table, before in snapshot.items():
        names = before["columns"]
        indices = [names.index(name) for name in before["primary"]]
        after = {}
        for row in connection.execute(
            f"SELECT {','.join(map(_quote, names))} FROM {_quote(table)}"
        ):
            identity = _row_hash(row[index] for index in indices) if indices else _row_hash(row)
            if table not in _APPEND_TABLES or identity in before["rows"]:
                record = after.setdefault(identity, [_row_hash(row), 0])
                record[1] += 1
        if after != before["rows"]:
            raise ValueError(
                "기존 설정·일정·알림·접근 권한 또는 기록이 변경되어 이관을 중단했습니다."
            )


def _presenter_map(path: Path | str | None) -> dict:
    if path is None:
        return {}
    path = Path(path)
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("발표자 매핑 파일 크기를 확인해주세요.")
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise ValueError("발표자 매핑은 이름과 Discord ID의 JSON 객체여야 합니다.")
    for name, identifier in mapping.items():
        _text(name, 200)
        _identifier(identifier, optional=True)
    return mapping


def _prepare(
    package: ImportPackage, connection: sqlite3.Connection, guild_id: int | None, mapping: dict
):
    manifest = package.manifest()
    batch_id = _key(manifest.get("batch_id"))
    if len(batch_id) > 100:
        raise ValueError("이관 모음 식별자가 너무 깁니다.")
    if guild_id is None:
        guilds = list(connection.execute("SELECT guild_id FROM guilds"))
        if len(guilds) != 1:
            raise ValueError("대상 서버를 --guild 옵션으로 지정해주세요.")
        guild_id = guilds[0][0]
    _identifier(guild_id)
    if not connection.execute("SELECT 1 FROM guilds WHERE guild_id=?", (guild_id,)).fetchone():
        raise ValueError("지정한 서버의 기존 봇 설정이 없습니다.")
    source_groups = manifest.get("groups")
    if not isinstance(source_groups, list) or not 1 <= len(source_groups) <= 1000:
        raise ValueError("이관할 발표 묶음을 확인해주세요.")
    groups, assets, keys, document_count = [], {}, set(), 0
    for raw in source_groups:
        if not isinstance(raw, dict):
            raise ValueError("발표 묶음 형식을 확인해주세요.")
        group_key = _key(raw.get("import_key"))
        if group_key in keys:
            raise ValueError("중복된 이관 식별자가 있습니다.")
        keys.add(group_key)
        name = _text(raw.get("presenter_name_snapshot"), 100)
        presenter = mapping.get(name, raw.get("presenter_id"))
        _identifier(presenter, optional=True)
        year = raw.get("seminar_year")
        if type(year) is not int or not 1000 <= year <= datetime.now(UTC).year:
            raise ValueError("과거 발표 연도를 확인해주세요.")
        value = raw.get("seminar_date")
        seminar_date = date.fromisoformat(value) if isinstance(value, str) else value
        if seminar_date is not None and (
            type(seminar_date) is not date
            or seminar_date.year != year
            or seminar_date > datetime.now(UTC).date()
        ):
            raise ValueError("과거 발표 날짜와 연도를 확인해주세요.")
        archive_kind = raw.get("archive_kind", "presentation")
        if (
            archive_kind not in {"presentation", "unassigned"}
            or archive_kind == "unassigned"
            and seminar_date is not None
        ):
            raise ValueError("회차 미확정 묶음에는 날짜를 지정할 수 없습니다.")
        group = {
            "import_key": group_key,
            "presenter_id": presenter,
            "presenter_name_snapshot": name,
            "seminar_year": year,
            "seminar_date": seminar_date,
            "archive_kind": archive_kind,
            "label": _text(raw.get("label", ""), 300, empty=True),
            "documents": [],
        }
        documents = raw.get("documents")
        if not isinstance(documents, list) or not documents:
            raise ValueError("이관할 자료 목록을 확인해주세요.")
        for source in documents:
            if not isinstance(source, dict):
                raise ValueError("자료 형식을 확인해주세요.")
            key = _key(source.get("import_key"))
            if key in keys:
                raise ValueError("중복된 이관 식별자가 있습니다.")
            keys.add(key)
            filename = _text(source.get("filename"), 240)
            if (
                filename in {".", ".."}
                or "/" in filename
                or "\\" in filename
                or len(filename.encode("utf-8")) > 240
                or PurePosixPath(filename).suffix.casefold() not in {".pdf", ".ppt", ".pptx"}
            ):
                raise ValueError("자료 파일명을 확인해주세요.")
            kind = source.get("kind")
            if kind not in {"paper", "slides"} or archive_kind == "unassigned" and kind != "paper":
                raise ValueError("자료 종류를 확인해주세요.")
            raw_info = source.get("info")
            if not isinstance(raw_info, dict) or set(raw_info) - set(
                DocumentInfo.__dataclass_fields__
            ):
                raise ValueError("논문 메타데이터 형식을 확인해주세요.")
            info = DocumentInfo(**raw_info)
            _text(info.title, 500)
            _text(info.extraction_method, 64)
            if info.text != "" or info.warning is not None and not isinstance(info.warning, str):
                raise ValueError("이관 패키지에는 논문 본문을 넣을 수 없습니다.")
            for value, maximum in ((info.doi, 255), (info.arxiv_id, 128), (info.warning, 1024)):
                if value is not None:
                    _text(value, maximum, empty=True)
            if info.sha256 != "" and (
                not isinstance(info.sha256, str) or not _SHA.fullmatch(info.sha256)
            ):
                raise ValueError("자료 SHA-256 형식을 확인해주세요.")
            asset = source.get("asset")
            file_path = ""
            if kind == "slides":
                asset = _member_name(asset)
                suffix = PurePosixPath(filename).suffix.casefold()
                if (
                    suffix not in {".pdf", ".ppt", ".pptx"}
                    or PurePosixPath(asset).suffix.casefold() != suffix
                ):
                    raise ValueError("발표 자료 형식을 확인해주세요.")
                file_path = f"files/{guild_id}/archive-import/{batch_id}/{info.sha256}{suffix}"
                if asset not in assets:
                    assets[asset] = {
                        "sha256": info.sha256,
                        "size": package.copy_verified(asset, info.sha256),
                    }
                elif assets[asset]["sha256"] != info.sha256:
                    raise ValueError("발표 자료 해시가 서로 다릅니다.")
            elif asset is not None:
                raise ValueError("논문 원문 파일은 이관 패키지에 포함할 수 없습니다.")
            if kind == "paper" and PurePosixPath(filename).suffix.casefold() != ".pdf":
                raise ValueError("논문 메타데이터에는 PDF 파일명이 필요합니다.")
            document = {
                "import_key": key,
                "info": info,
                "filename": filename,
                "kind": kind,
                "file_path": file_path,
                "_asset": asset,
            }
            for field, maximum in (("venue", 200), ("link", 2048)):
                field_value = source.get(field)
                if field_value is not None:
                    _text(field_value, maximum, empty=True)
                document[field] = field_value
            publication_year = source.get("year")
            if publication_year is not None and (
                type(publication_year) is not int
                or not 1000 <= publication_year <= datetime.now(UTC).year + 2
            ):
                raise ValueError("논문 발행 연도를 확인해주세요.")
            document["year"] = publication_year
            validate_paper_fields(
                {
                    "title": info.title,
                    "doi": info.doi,
                    "venue": document["venue"],
                    "year": document["year"],
                    "link": document["link"],
                }
            )
            group["documents"].append(document)
            document_count += 1
            if document_count > 10_000:
                raise ValueError("이관 자료 수 제한을 초과했습니다.")
        groups.append(group)
    if set(package.members) != {"manifest.json", *assets}:
        raise ValueError("패키지에는 manifest와 검증된 발표 자료만 포함할 수 있습니다.")
    return batch_id, guild_id, groups, assets


def _preview(connection, batch_id, guild_id, groups, assets):
    tables = _tables(connection)
    old_groups = (
        {
            row[0]
            for row in connection.execute(
                "SELECT import_key FROM archive_presentations WHERE guild_id=?", (guild_id,)
            )
        }
        if "archive_presentations" in tables
        else set()
    )
    old_documents = (
        {
            row[0]
            for row in connection.execute(
                "SELECT import_key FROM archive_import_items WHERE guild_id=?", (guild_id,)
            )
        }
        if "archive_import_items" in tables
        else set()
    )
    tombstones = (
        {
            row[0]
            for row in connection.execute(
                "SELECT upload_key FROM web_upload_tombstones WHERE guild_id=?", (guild_id,)
            )
        }
        if "web_upload_tombstones" in tables
        else set()
    )
    skipped = sum(
        document["import_key"] in old_documents | tombstones
        for group in groups
        for document in group["documents"]
    )
    return {
        "applied": False,
        "batch_id": batch_id,
        "groups": len(groups),
        "presentation_groups": sum(group["archive_kind"] == "presentation" for group in groups),
        "unassigned_groups": sum(group["archive_kind"] == "unassigned" for group in groups),
        "documents": sum(len(group["documents"]) for group in groups),
        "slides": sum(
            document["kind"] == "slides" for group in groups for document in group["documents"]
        ),
        "papers": sum(
            document["kind"] == "paper" for group in groups for document in group["documents"]
        ),
        "dated_groups": sum(group["seminar_date"] is not None for group in groups),
        "unresolved_presenter_groups": sum(group["presenter_id"] is None for group in groups),
        "asset_files": len(assets),
        "asset_bytes": sum(asset["size"] for asset in assets.values()),
        "would_insert_groups": sum(group["import_key"] not in old_groups for group in groups),
        "would_insert_documents": sum(len(group["documents"]) for group in groups) - skipped,
        "would_skip_documents": skipped,
        "database_schema": connection.execute("PRAGMA user_version").fetchone()[0],
    }


def _backup(connection: sqlite3.Connection, directory: Path, batch_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"archive-before-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{hashlib.sha256(batch_id.encode()).hexdigest()[:12]}-{uuid.uuid4().hex[:8]}.sqlite3"
    path = directory / name
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    target = sqlite3.connect(path)
    try:
        connection.backup(target)
    finally:
        target.close()
    return path


def _validate_on_copy(
    connection: sqlite3.Connection, guild: int, groups: list[dict], data_root: Path
) -> dict:
    """Exercise migrations, conflicts and the real Store path on a disposable copy."""
    from .store import Store

    snapshot = runtime_snapshot(connection)
    with tempfile.TemporaryDirectory(prefix="seminar-import-check-") as directory:
        copied = Path(directory) / "check.sqlite3"
        target = sqlite3.connect(copied)
        try:
            connection.backup(target)
        finally:
            target.close()
        copied.chmod(0o600)
        store = Store(copied)
        try:
            verify_runtime_snapshot(store.db, snapshot)
            result = store.insert_archive_batch(
                guild, groups, imported_at=datetime.now(UTC), commit=False
            )
            _verify_existing_assets(store.db, guild, groups, result, data_root)
            verify_runtime_snapshot(store.db, snapshot)
            store.db.rollback()
        finally:
            store.close()
    return {
        "validated_on_isolated_copy": True,
        "would_insert_groups": result["inserted_groups"],
        "would_insert_documents": result["inserted_documents"],
        "would_skip_documents": result["skipped_documents"],
        "would_skip_deleted_documents": result.get("tombstoned_documents", 0),
    }


def _destination(root: Path, relative: str, *, create: bool = True) -> Path:
    path = root / _member_name(relative)
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("발표 자료 저장 경로에 심볼릭 링크가 있습니다.")
        if create:
            current.mkdir(exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("발표 자료 저장 경로에 심볼릭 링크가 있습니다.")
    return path


def _verify_existing_assets(connection, guild, groups, result, data_root):
    prepared = {
        document["import_key"]: document for group in groups for document in group["documents"]
    }
    for item in result["documents"]:
        document = prepared[item["import_key"]]
        if document["kind"] != "slides" or item["created"] or item.get("tombstoned"):
            continue
        stored = connection.execute(
            "SELECT kind,file_path,sha256 FROM documents WHERE guild_id=? AND id=?",
            (guild, item["id"]),
        ).fetchone()
        if (
            stored is None
            or stored["kind"] != "slides"
            or stored["sha256"] != document["info"].sha256
        ):
            raise ValueError("기존 이관 발표 자료의 파일 정보를 확인해주세요.")
        path = _destination(data_root, stored["file_path"], create=False)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_ASSET_BYTES:
            raise ValueError("기존 이관 발표 자료 파일이 없거나 크기가 변경되었습니다.")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != stored["sha256"]:
                raise ValueError("기존 이관 발표 자료의 SHA-256이 다릅니다.")


def run_import(
    package_path: Path | str,
    database: Path | str,
    *,
    guild_id: int | None = None,
    presenter_map: Path | str | None = None,
    apply: bool = False,
    backup_dir: Path | str | None = None,
) -> dict:
    database = Path(database).resolve()
    mapping = _presenter_map(presenter_map)
    with ImportPackage(package_path) as package:
        if not apply:
            with _readonly(database) as connection:
                batch, guild, groups, assets = _prepare(package, connection, guild_id, mapping)
                return {
                    **_preview(connection, batch, guild, groups, assets),
                    **_validate_on_copy(connection, guild, groups, database.parent),
                }
        from .__main__ import instance_lock
        from .store import Store

        with instance_lock(database.parent):
            with _readonly(database) as connection:
                batch, guild, groups, assets = _prepare(package, connection, guild_id, mapping)
                preview = _preview(connection, batch, guild, groups, assets)
                preview.update(_validate_on_copy(connection, guild, groups, database.parent))
                snapshot = runtime_snapshot(connection)
                backup = _backup(
                    connection,
                    Path(backup_dir) if backup_dir else database.parent / "backups",
                    batch,
                )
            created_files = []
            store = None
            committed = False
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".archive-import-", dir=database.parent
                ) as temporary:
                    staged = {}
                    for index, (asset, metadata) in enumerate(assets.items()):
                        staged[asset] = Path(temporary) / str(index)
                        package.copy_verified(asset, metadata["sha256"], staged[asset])
                    store = Store(database)
                    verify_runtime_snapshot(store.db, snapshot)
                    result = store.insert_archive_batch(
                        guild, groups, imported_at=datetime.now(UTC), commit=False
                    )
                    _verify_existing_assets(store.db, guild, groups, result, database.parent)
                    if not store.db.in_transaction:
                        raise ValueError("이관 트랜잭션을 확인할 수 없습니다.")
                    created_keys = {
                        item["import_key"] for item in result["documents"] if item["created"]
                    }
                    for group in groups:
                        for document in group["documents"]:
                            if (
                                document["kind"] != "slides"
                                or document["import_key"] not in created_keys
                            ):
                                continue
                            destination = _destination(database.parent, document["file_path"])
                            staged_file = staged[document["_asset"]]
                            if destination.exists():
                                with destination.open("rb") as current:
                                    digest = hashlib.file_digest(current, "sha256").hexdigest()
                                if (
                                    destination.stat().st_size != staged_file.stat().st_size
                                    or digest != document["info"].sha256
                                ):
                                    raise ValueError("기존 발표 자료 파일과 이관 자료가 다릅니다.")
                            else:
                                os.link(staged_file, destination)
                                created_files.append(destination)
                    verify_runtime_snapshot(store.db, snapshot)
                    store.db.commit()
                    committed = True
                verify_runtime_snapshot(store.db, snapshot)
                return {
                    **preview,
                    "applied": True,
                    "backup": str(backup),
                    "inserted_groups": result["inserted_groups"],
                    "inserted_documents": result["inserted_documents"],
                    "skipped_documents": result["skipped_documents"],
                    "runtime_preserved": True,
                }
            except BaseException:
                if store:
                    store.db.rollback()
                if not committed:
                    for path in created_files:
                        path.unlink(missing_ok=True)
                raise
            finally:
                if store:
                    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="검토된 과거 세미나 자료를 별도 아카이브로 이관합니다. 기본값은 읽기 전용 사전 점검입니다."
    )
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument(
        "--database", type=Path, default=Path(os.getenv("DATA_DIR", "./data")) / "seminar.sqlite3"
    )
    parser.add_argument(
        "--guild", "--guild-id", dest="guild_id", type=int, default=os.getenv("SEMINAR_GUILD_ID")
    )
    parser.add_argument(
        "--presenter-map",
        type=Path,
        help="발표자 표시 이름을 Discord ID 또는 null로 연결한 비공개 JSON",
    )
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="봇을 중지한 뒤 실제 반영합니다. SQLite 백업과 기존 운영 상태 보존 검증을 수행합니다.",
    )
    args = parser.parse_args()
    try:
        result = run_import(
            args.package,
            args.database,
            guild_id=args.guild_id,
            presenter_map=args.presenter_map,
            apply=args.apply,
            backup_dir=args.backup_dir,
        )
    except (ValueError, TypeError, OSError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(
            2,
            f"이관을 완료하지 못했습니다: {type(error).__name__}: {error}\n",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
