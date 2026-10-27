"""SQLite backup, verification, restore and rollback for the Placement Engine (Phase 1.2).

deploy/backup.sh and deploy/restore.sh pipe this file to `python -` inside a
one-off container of the `migrate` service, so it runs as the app user (10001)
with no network and needs no change to the image. Standard library only, Python 3.12.

    backup   --dest DIR [--keep N] [--tag NAME]   live DB -> verified placement-<UTC>.db.gz
    verify   ARCHIVE --workdir DIR                 decompress + check, change nothing
    restore  ARCHIVE                               replace the DB (api and worker stopped)
    rollback DIR                                   undo a restore from data/pre-restore-<UTC>/

The live database is only ever opened read-only, and the snapshot is taken with
`VACUUM INTO`: one read transaction, so the API and worker keep writing (WAL
readers never block writers) and the copy comes out compacted. A plain VACUUM
would rewrite the live file under an exclusive lock; nothing here runs one.
The database path comes from DATABASE_URL, the value docker-compose.yml sets.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ARCHIVE_RE = re.compile(r"^placement-(\d{8}T\d{6}Z)(?:-([a-z0-9][a-z0-9-]{0,31}))?\.db\.gz$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
URL_PREFIXES = ("sqlite+aiosqlite:///", "sqlite:///")
SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
CHUNK = 1024 * 1024
BUSY_TIMEOUT_SECONDS = 30


class BackupError(Exception):
    """A check failed; nothing was replaced or rotated."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def db_path_from_url(url: str) -> Path:
    for prefix in URL_PREFIXES:
        if url.startswith(prefix):
            path = url[len(prefix):].split("?", 1)[0]
            break
    else:
        raise BackupError(f"DATABASE_URL is not a SQLite URL: {url.split(':', 1)[0]}:...")
    if not path.startswith("/"):  # also rejects :memory: and relative paths
        raise BackupError("DATABASE_URL must name an absolute SQLite file path")
    return Path(path)


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"database not found: {path}")
    # Autocommit: VACUUM INTO cannot run inside the transaction the sqlite3 module would open.
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None
    )


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "posix":  # directories cannot be opened for fsync on Windows (dev runs)
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def check_database(path: Path, *, require_schema: bool = False) -> dict:
    """PRAGMA integrity_check plus a summary. Raises BackupError unless the result is 'ok'."""
    try:
        conn = connect_readonly(path)
        try:
            result = [row[0] for row in conn.execute("PRAGMA integrity_check")]
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            rows = {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
            revision = None
            if "alembic_version" in tables:
                found = conn.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = found[0] if found else None
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{path.name}: not a readable SQLite database ({exc})") from exc
    if result != ["ok"]:
        raise BackupError(f"{path.name}: integrity_check failed: {'; '.join(result[:5])}")
    if require_schema and revision is None:
        raise BackupError(f"{path.name}: no alembic_version row, so not a Placement Engine database")
    return {"revision": revision, "tables": len(tables), "rows": rows}


def _compress(src: Path, dst: Path) -> str:
    digest = hashlib.sha256()
    with open(src, "rb") as fin, open(dst, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0) as fout:
            while chunk := fin.read(CHUNK):
                digest.update(chunk)
                fout.write(chunk)
        raw.flush()
        os.fsync(raw.fileno())
    return digest.hexdigest()


def _decompress(src: Path, dst: Path | None) -> str:
    """Stream src through gzip (CRC and length checked at EOF); write to dst if given."""
    digest = hashlib.sha256()
    try:
        with gzip.open(src, "rb") as fin:
            out = open(dst, "wb") if dst else None
            try:
                while chunk := fin.read(CHUNK):
                    digest.update(chunk)
                    if out:
                        out.write(chunk)
                if out:
                    out.flush()
                    os.fsync(out.fileno())
            finally:
                if out:
                    out.close()
    except (OSError, EOFError) as exc:  # BadGzipFile is an OSError
        raise BackupError(f"{src.name}: damaged archive ({exc})") from exc
    return digest.hexdigest()


def _remove_temp_files(directory: Path) -> None:
    # Leftovers of a killed run; backup.sh holds a lock, so none belong to a live run.
    for pattern in (".placement-*.tmp", ".placement-*.partial", ".verify-*.tmp", ".restore-*.tmp"):
        for leftover in directory.glob(pattern):
            leftover.unlink(missing_ok=True)


def backup(source: sqlite3.Connection, dest: Path, *, keep: int, tag: str | None = None, source_bytes: int = 0) -> tuple[Path, dict, list[Path]]:
    """Snapshot `source` into dest/placement-<UTC>[-tag].db.gz, then rotate scheduled backups."""
    if tag is not None and not TAG_RE.match(tag):
        raise BackupError(f"invalid tag {tag!r}: lowercase letters, digits and '-', at most 32")
    if keep < 1:
        raise BackupError("keep must be at least 1")
    _remove_temp_files(dest)
    # Room for the uncompressed snapshot and its archive at the same time.
    free = shutil.disk_usage(dest).free
    if free < 2 * source_bytes + 64 * CHUNK:
        raise BackupError(f"not enough free space in {dest}: {free} bytes free, database is {source_bytes} bytes")

    stamp = _stamp()
    name = f"placement-{stamp}{'-' + tag if tag else ''}.db.gz"
    final = dest / name
    if final.exists():
        raise BackupError(f"{name} already exists")
    tmp_db = dest / f".placement-{stamp}.db.tmp"
    tmp_gz = dest / f".placement-{stamp}.db.gz.partial"
    try:
        source.execute("VACUUM INTO ?", (str(tmp_db),))
        summary = check_database(tmp_db)
        digest = _compress(tmp_db, tmp_gz)
        if _decompress(tmp_gz, None) != digest:
            raise BackupError("archive does not decompress to the snapshot")
        os.replace(tmp_gz, final)
        _fsync_dir(dest)
    finally:
        tmp_db.unlink(missing_ok=True)
        tmp_gz.unlink(missing_ok=True)
    summary["sha256"] = digest
    return final, summary, rotate(dest, keep)


def rotate(dest: Path, keep: int) -> list[Path]:
    """Keep the newest `keep` scheduled (untagged) archives. Tagged ones are never rotated."""
    scheduled = sorted(
        (p for p in dest.iterdir() if p.is_file() and (m := ARCHIVE_RE.match(p.name)) and m.group(2) is None),
        key=lambda p: p.name,
        reverse=True,
    )
    removed = scheduled[keep:]
    for old in removed:
        old.unlink()
    return removed


def resolve_archive(directory: Path, name: str) -> Path:
    """Only a bare archive name inside the backups directory, never a path."""
    if not ARCHIVE_RE.match(name):
        raise BackupError(f"not a backup archive name: {name!r} (expected placement-<YYYYmmddTHHMMSSZ>[-tag].db.gz)")
    archive = directory / name
    if not archive.is_file():
        raise BackupError(f"no such archive: {archive}")
    return archive


def verify(archive: Path, workdir: Path) -> dict:
    tmp = workdir / f".verify-{_stamp()}.tmp"
    try:
        digest = _decompress(archive, tmp)
        summary = check_database(tmp, require_schema=True)
    finally:
        tmp.unlink(missing_ok=True)
    summary["sha256"] = digest
    return summary


def _database_set(db: Path) -> list[Path]:
    return [p for p in (db, *(db.with_name(db.name + s) for s in SIDE_SUFFIXES)) if p.exists()]


def _move_set(files: list[Path], directory: Path) -> list[tuple[Path, Path]]:
    moved = []
    for f in files:
        target = directory / f.name
        os.replace(f, target)
        moved.append((f, target))
    return moved


def restore(archive: Path, db: Path) -> tuple[Path | None, dict]:
    """Replace db with the archive. The current db and its -wal/-shm move, together,
    into data/pre-restore-<UTC>/: a stale WAL next to a different db file would be
    replayed into it. Callers must stop every process using the database first."""
    data_dir = db.parent
    stamp = _stamp()
    tmp = data_dir / f".restore-{stamp}.tmp"
    try:
        digest = _decompress(archive, tmp)
        summary = check_database(tmp, require_schema=True)
        current = _database_set(db)
        aside = None
        if current:
            aside = data_dir / f"pre-restore-{stamp}"
            aside.mkdir(mode=0o700)
            moved = _move_set(current, aside)
        try:
            os.replace(tmp, db)
        except OSError:
            if current:
                for original, target in moved:
                    os.replace(target, original)
            raise
        _fsync_dir(data_dir)
    finally:
        tmp.unlink(missing_ok=True)
    summary["sha256"] = digest
    return aside, summary


def rollback(saved: Path, db: Path) -> Path | None:
    """Put a pre-restore-<UTC>/ set back; the database it replaces moves to rolled-back-<UTC>/."""
    if not re.fullmatch(r"pre-restore-\d{8}T\d{6}Z", saved.name) or saved.parent != db.parent:
        raise BackupError(f"not a pre-restore directory in {db.parent}: {saved.name!r}")
    if not (saved / db.name).is_file():
        raise BackupError(f"{saved.name} holds no {db.name}")
    check_database(saved / db.name)
    current = _database_set(db)
    aside = None
    if current:
        aside = db.parent / f"rolled-back-{_stamp()}"
        aside.mkdir(mode=0o700)
        _move_set(current, aside)
    _move_set(_database_set(saved / db.name), db.parent)
    try:
        saved.rmdir()
    except OSError:
        pass  # something else was put in there; leave it for the owner
    _fsync_dir(db.parent)
    return aside


def _describe(summary: dict) -> str:
    total = sum(summary["rows"].values())
    return f"revision={summary['revision']} tables={summary['tables']} rows={total} sha256={summary['sha256'][:16]}..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlite_backup", description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, help="database file (default: from DATABASE_URL)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--keep", type=int, default=14)
    p.add_argument("--tag")
    p = sub.add_parser("verify")
    p.add_argument("archive")
    p.add_argument("--workdir", type=Path, required=True)
    p = sub.add_parser("restore")
    p.add_argument("archive")
    p.add_argument("--archives", type=Path, required=True)
    p = sub.add_parser("rollback")
    p.add_argument("directory")
    p = sub.add_parser("list")
    p.add_argument("--archives", type=Path, required=True)
    args = parser.parse_args(argv)

    os.umask(0o077)
    try:
        db = args.db or db_path_from_url(os.environ.get("DATABASE_URL", ""))
        if args.command == "backup":
            source = connect_readonly(db)
            try:
                size = sum(p.stat().st_size for p in _database_set(db))
                archive, summary, removed = backup(source, args.dest, keep=args.keep, tag=args.tag, source_bytes=size)
            finally:
                source.close()
            print(f"backup ok: {archive.name} ({archive.stat().st_size} bytes) {_describe(summary)}")
            for old in removed:
                print(f"rotated out: {old.name}")
        elif args.command == "verify":
            archive = resolve_archive(args.workdir, args.archive)
            summary = verify(archive, args.workdir)
            print(f"verify ok: {archive.name} {_describe(summary)}")
            for table, count in sorted(summary["rows"].items()):
                print(f"  {table}: {count}")
        elif args.command == "restore":
            archive = resolve_archive(args.archives, args.archive)
            aside, summary = restore(archive, db)
            print(f"restore ok: {db.name} <- {archive.name} {_describe(summary)}")
            print(f"previous database kept in: {aside.name}" if aside else "there was no previous database")
        elif args.command == "rollback":
            aside = rollback(db.parent / args.directory, db)
            print(f"rollback ok: {args.directory} is live again")
            if aside:
                print(f"the restored database was moved to: {aside.name}")
        elif args.command == "list":
            for archive in sorted(args.archives.iterdir(), key=lambda p: p.name, reverse=True):
                if archive.is_file() and ARCHIVE_RE.match(archive.name):
                    print(f"{archive.name}  {archive.stat().st_size} bytes")
            for saved in sorted(db.parent.iterdir()):
                if saved.is_dir() and re.fullmatch(r"(pre-restore|rolled-back)-\d{8}T\d{6}Z", saved.name):
                    print(f"data/{saved.name}/  {', '.join(sorted(f.name for f in saved.iterdir()))}")
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
