"""
verify_backup.py
Phase 1.2 — SQLite backup, restore and rollback verification suite.

Covers:
  • backup: a VACUUM INTO snapshot that passes integrity_check, gzip-compressed,
    identical to the source, mode 0600, no temporary files left behind
  • a writer holding an open transaction neither blocks the backup nor leaks
    uncommitted rows into it; the live file is opened read-only
  • rotation keeps the newest N scheduled archives and never touches tagged
    archives or unrelated files
  • every failure (integrity, damaged archive, disk space, bad arguments) leaves no
    archive, no temporary file and nothing rotated
  • restore moves the database and its -wal/-shm files aside together, works on an
    empty data directory, refuses a non-Placement database, and rollback undoes it
  • archive names and rollback directories are bare names, never paths; the
    database path comes from the compose DATABASE_URL
  • backup.sh / restore.sh / systemd units: strict bash, one lock, the `migrate`
    container with no session or secrets mounted, LF line endings, oneshot service
    as the deploy user, persistent daily timer

Every database is created from sqlite3 ":memory:". The parts that are about files
(archives, the restore target, read-only opening, WAL locking) use throwaway files
inside a TemporaryDirectory; dev.db is never opened.
Run: python verify_backup.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import ast
import contextlib
import gzip
import importlib.util
import io
import itertools
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parent
DEPLOY = ROOT / "deploy"

_spec = importlib.util.spec_from_file_location("sqlite_backup", DEPLOY / "sqlite_backup.py")
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

_clock = itertools.count(1)


def _fake_stamp() -> str:
    """Distinct, increasing stamps: several backups run within one second here."""
    return f"20991231T{next(_clock):06d}Z"


sb._stamp = _fake_stamp


def _memory_db(marker: str, *, schema: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    if schema:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES ('0001_test')")
    conn.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, company_id INTEGER REFERENCES companies(id), title TEXT)")
    conn.executemany("INSERT INTO companies (name) VALUES (?)", [(f"{marker}-{i}",) for i in range(50)])
    conn.executemany("INSERT INTO events (company_id, title) VALUES (?, ?)", [(i % 50 + 1, "x" * 200) for i in range(500)])
    conn.commit()
    return conn


def _write_db(conn: sqlite3.Connection, path: Path) -> Path:
    conn.execute("VACUUM INTO ?", (str(path),))
    return path


def _rows(path: Path) -> list:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT name FROM companies ORDER BY id").fetchall()
    finally:
        conn.close()


def _gunzip_to(archive: Path, target: Path) -> Path:
    with gzip.open(archive, "rb") as fin, open(target, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return target


def _expect_error(fn, *args, contains: str = "", **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except sb.BackupError as exc:
        assert contains in str(exc), f"expected {contains!r} in {exc}"
        return str(exc)
    raise AssertionError(f"{fn.__name__} should have failed")


def _listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_backup_is_verified_compressed_and_identical() -> None:
    source = _memory_db("live")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        archive, summary, removed = sb.backup(source, dest, keep=3)
        assert sb.ARCHIVE_RE.match(archive.name) and removed == [], archive.name
        assert _listing(dest) == [archive.name], _listing(dest)  # no .tmp / .partial left
        assert summary["revision"] == "0001_test" and summary["rows"]["events"] == 500, summary
        if os.name == "posix":
            assert archive.stat().st_mode & 0o777 == 0o600, oct(archive.stat().st_mode)
        restored = _gunzip_to(archive, dest / "check.db")
        assert _rows(restored) == source.execute("SELECT name FROM companies ORDER BY id").fetchall()
        assert archive.stat().st_size < restored.stat().st_size, "archive is not compressed"
        tagged, _, _ = sb.backup(source, dest, keep=3, tag="pre-deploy")
        assert tagged.name.endswith("-pre-deploy.db.gz"), tagged.name
    source.close()
    print("  [1] ✓ backup: VACUUM INTO snapshot passes integrity_check, is gzip-compressed, matches the source; no temporary files left")


def test_live_writer_is_not_blocked_and_not_leaked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        live = _write_db(_memory_db("live"), tmp / "placement.db")
        writer = sqlite3.connect(live, timeout=0)
        try:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            dest = tmp / "backups"
            dest.mkdir()
            source = sb.connect_readonly(live)
            try:
                with contextlib.suppress(sqlite3.OperationalError):
                    source.execute("INSERT INTO companies (name) VALUES ('via backup connection')")
                    raise AssertionError("the backup connection could write to the live database")
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO companies (name) VALUES ('uncommitted')")
                archive, _, _ = sb.backup(source, dest, keep=3)
            finally:
                source.close()
            writer.commit()  # the writer was never blocked
            names = [r[0] for r in _rows(_gunzip_to(archive, tmp / "check.db"))]
            assert "uncommitted" not in names and len(names) == 50, len(names)
        finally:
            writer.close()
    print("  [2] ✓ a writer mid-transaction is not blocked; the snapshot holds only committed rows; the live file is opened read-only")


def test_rotation_keeps_newest_scheduled_only() -> None:
    source = _memory_db("live")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        old = [f"placement-2026010{i}T030000Z.db.gz" for i in range(1, 6)]
        kept_forever = ["placement-20260101T000000Z-pre-deploy.db.gz", "placement-20251201T000000Z-manual.db.gz"]
        unrelated = ["notes.txt", "placement-latest.db.gz", "placement-20260101T030000Z.db"]
        for name in old + kept_forever + unrelated:
            (dest / name).write_bytes(b"x")
        archive, _, removed = sb.backup(source, dest, keep=3)
        assert sorted(p.name for p in removed) == old[:3], removed
        assert _listing(dest) == sorted([archive.name, old[3], old[4]] + kept_forever + unrelated), _listing(dest)
        _expect_error(sb.backup, source, dest, keep=0, contains="keep")
        for bad in ("Pre-Deploy", "../x", "a" * 33, "-x", ""):
            _expect_error(sb.backup, source, dest, keep=3, tag=bad, contains="invalid tag")
    source.close()
    print("  [3] ✓ rotation keeps the newest N scheduled archives; tagged archives and other files are never removed; bad tags/keep refused")


def test_failures_leave_nothing_behind() -> None:
    source = _memory_db("live")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        (dest / "placement-20260101T030000Z.db.gz").write_bytes(b"x")
        (dest / ".placement-20260101T000000Z.db.tmp").write_bytes(b"left by a killed run")
        before = ["placement-20260101T030000Z.db.gz"]

        real_check = sb.check_database
        sb.check_database = lambda *a, **k: (_ for _ in ()).throw(sb.BackupError("integrity_check failed: test"))
        try:
            _expect_error(sb.backup, source, dest, keep=1, contains="integrity_check")
        finally:
            sb.check_database = real_check
        assert _listing(dest) == before, _listing(dest)  # nothing written or rotated; stale temp removed

        real_usage = sb.shutil.disk_usage
        sb.shutil.disk_usage = lambda path: SimpleNamespace(free=1024)
        try:
            _expect_error(sb.backup, source, dest, keep=1, source_bytes=10 * 1024 * 1024, contains="free space")
        finally:
            sb.shutil.disk_usage = real_usage
        assert _listing(dest) == before, _listing(dest)

        # A really damaged database file fails the check rather than being archived.
        damaged = _write_db(source, dest / "damaged.db")
        data = bytearray(damaged.read_bytes())
        data[4096:8192] = os.urandom(4096)
        damaged.write_bytes(bytes(data))
        _expect_error(sb.check_database, damaged)

        # A file that opens fine but fails integrity_check (an index that no longer
        # matches its table) is refused on the check's result, not on an exception.
        inconsistent = _write_db(source, dest / "inconsistent.db")
        conn = sqlite3.connect(inconsistent)
        conn.execute("CREATE INDEX companies_name ON companies(name)")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute("UPDATE sqlite_master SET sql = 'CREATE INDEX companies_name ON companies(id)' WHERE name = 'companies_name'")
        conn.commit()
        conn.close()
        _expect_error(sb.check_database, inconsistent, contains="integrity_check failed: row")
        for leftover in ("damaged.db", "inconsistent.db"):
            (dest / leftover).unlink()

        # An archive that does not decompress to the snapshot is not kept either.
        real_compress = sb._compress

        def _corrupting_compress(src, dst):
            digest = real_compress(src, dst)
            data = bytearray(dst.read_bytes())
            data[len(data) // 2] ^= 0xFF
            dst.write_bytes(bytes(data))
            return digest

        sb._compress = _corrupting_compress
        try:
            _expect_error(sb.backup, source, dest, keep=1)
        finally:
            sb._compress = real_compress
        assert _listing(dest) == before, _listing(dest)

        # A truncated archive is caught by gzip's length/CRC check.
        good, _, _ = sb.backup(source, dest, keep=5)
        truncated = dest / "placement-20260102T030000Z.db.gz"
        truncated.write_bytes(good.read_bytes()[:-100])
        _expect_error(sb.verify, truncated, dest, contains="damaged archive")
        assert not list(dest.glob(".verify-*")), _listing(dest)
    source.close()
    print("  [4] ✓ failed integrity check, low disk space, a damaged database or archive: no archive kept, nothing rotated, no temporary files")


def test_restore_and_rollback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data, archives = tmp / "data", tmp / "backups"
        data.mkdir()
        archives.mkdir()
        db = data / "placement.db"
        archive, _, _ = sb.backup(_memory_db("from-backup"), archives, keep=5)

        # Empty data directory (a new VM): nothing to move aside.
        aside, summary = sb.restore(archive, db)
        assert aside is None and summary["revision"] == "0001_test" and _listing(data) == ["placement.db"], _listing(data)
        db.unlink()

        # A live set with -wal/-shm: all three move together.
        _write_db(_memory_db("current"), db)
        for suffix in ("-wal", "-shm"):
            db.with_name(db.name + suffix).write_bytes(b"stale side file")
        aside, _ = sb.restore(archive, db)
        assert _listing(data) == sorted(["placement.db", aside.name]), _listing(data)
        assert _listing(aside) == ["placement.db", "placement.db-shm", "placement.db-wal"], _listing(aside)
        assert _rows(db)[0] == ("from-backup-0",) and _rows(aside / "placement.db")[0] == ("current-0",)

        rolled = sb.rollback(aside, db)
        assert not aside.exists(), "pre-restore directory left behind"
        assert _listing(data) == sorted(["placement.db", "placement.db-shm", "placement.db-wal", rolled.name]), _listing(data)
        assert _rows(db)[0] == ("current-0",) and _rows(rolled / "placement.db")[0] == ("from-backup-0",)
    print("  [5] ✓ restore moves the db and its -wal/-shm aside together, works on an empty data dir; rollback puts them back")


def test_restore_refusals_change_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data, archives = tmp / "data", tmp / "backups"
        data.mkdir()
        archives.mkdir()
        db = _write_db(_memory_db("current"), data / "placement.db")
        foreign, _, _ = sb.backup(_memory_db("other", schema=False), archives, keep=5)
        _expect_error(sb.restore, foreign, db, contains="alembic_version")
        _expect_error(sb.verify, foreign, archives, contains="alembic_version")
        assert _listing(data) == ["placement.db"] and _rows(db)[0] == ("current-0",), _listing(data)

        for name in ("../placement-20260101T030000Z.db.gz", "/etc/passwd", "placement.db", foreign.name + "/x", ""):
            _expect_error(sb.resolve_archive, archives, name, contains="not a backup archive name")
        _expect_error(sb.resolve_archive, archives, "placement-20200101T000000Z.db.gz", contains="no such archive")
        for name in ("..", "rolled-back-20260101T000000Z", "pre-restore-x", "pre-restore-20260101T000000Z"):
            _expect_error(sb.rollback, data / name, db)
        _expect_error(sb.rollback, tmp / "pre-restore-20260101T000000Z", db, contains="not a pre-restore directory")
    print("  [6] ✓ a non-Placement database is refused; archives are bare names in the backups dir; rollback only takes data/pre-restore-<UTC>")


def test_cli_and_database_url() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    url = compose["x-database-url"]
    assert sb.db_path_from_url(url).as_posix() == "/var/lib/placement/data/placement.db", url
    for bad in ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///./dev.db", "postgresql+asyncpg://u@h/db", ""):
        _expect_error(sb.db_path_from_url, bad)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data, archives = tmp / "data", tmp / "backups"
        data.mkdir()
        archives.mkdir()
        db = _write_db(_memory_db("current"), data / "placement.db")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert sb.main(["--db", str(db), "backup", "--dest", str(archives), "--keep", "2"]) == 0
            name = next(p.name for p in archives.iterdir())
            assert sb.main(["--db", str(db), "verify", name, "--workdir", str(archives)]) == 0
            assert sb.main(["--db", str(db), "restore", name, "--archives", str(archives)]) == 0
            assert sb.main(["--db", str(db), "list", "--archives", str(archives)]) == 0
        text = out.getvalue()
        assert "backup ok: placement-" in text and "verify ok:" in text and "companies: 50" in text, text
        assert "restore ok:" in text and "previous database kept in: pre-restore-" in text and "data/pre-restore-" in text, text
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert sb.main(["--db", str(db), "verify", "../x.db.gz", "--workdir", str(archives)]) == 1
            assert sb.main(["--db", str(data / "missing.db"), "backup", "--dest", str(archives)]) == 1
        assert "ERROR: not a backup archive name" in err.getvalue() and "database not found" in err.getvalue(), err.getvalue()
    print("  [7] ✓ CLI backup / verify / restore / list; DATABASE_URL from docker-compose.yml; errors exit 1 with a message")


def _shell(name: str) -> str:
    raw = (DEPLOY / name).read_bytes()
    assert b"\r" not in raw, f"{name} has CRLF line endings"
    return raw.decode("utf-8")


def test_scripts_and_units() -> None:
    helper = (DEPLOY / "sqlite_backup.py").read_text(encoding="utf-8")
    # Its output goes to journald and whatever console runs restore.sh: a non-ASCII
    # character there once turned a finished restore into a reported failure.
    assert helper.isascii(), "deploy/sqlite_backup.py must stay ASCII"
    tree = ast.parse(helper)
    vacuums = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.lstrip().upper().startswith("VACUUM")]
    assert vacuums == ["VACUUM INTO ?"], vacuums  # never a plain VACUUM of the live file
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)

    for name in ("backup.sh", "restore.sh"):
        text = _shell(name)
        assert text.startswith("#!/usr/bin/env bash\n") and "\nset -Eeuo pipefail\n" in text, name
        assert 'exec 9<"$HELPER"' in text and "flock -n 9" in text, f"{name}: not on the shared lock"
        assert "run --rm --no-deps -T" in text and " migrate python - " in text, f"{name}: not the migrate container"
        volumes = [line for line in text.splitlines() if "--volume" in line and not line.lstrip().startswith("#")]
        assert volumes and all("$BACKUP_DIR:$MOUNTED" in line for line in volumes), volumes
        code = text.split("set -Eeuo pipefail", 1)[1]
        assert "/telegram" not in code and "/secrets" not in code, f"{name} touches the session or secrets"
        assert "sudo" not in code, f"{name} runs sudo"
        if os.name == "posix" and shutil.which("bash"):
            subprocess.run(["bash", "-n", str(DEPLOY / name)], check=True)

    units = {}
    for name in ("placement-backup.service", "placement-backup.timer"):
        text = _shell(f"systemd/{name}")
        for line in text.splitlines():
            if line and not line.startswith("#"):
                assert "#" not in line, f"{name}: inline comment becomes part of the value: {line}"
        units[name] = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in text.splitlines() if "=" in line and not line.startswith("#")}
    service, timer = units["placement-backup.service"], units["placement-backup.timer"]
    assert service["Type"] == "oneshot" and service["User"] not in ("root", "0"), service
    assert service["ExecStart"].endswith("/deploy/backup.sh") and service["NoNewPrivileges"] == "yes", service
    assert "docker.service" in service["Requires"] and "docker.service" in service["After"], service
    assert service["Environment"] and service["WorkingDirectory"] == "/srv/placement/app", service
    assert timer["Persistent"] == "true" and timer["OnCalendar"] and timer["WantedBy"] == "timers.target", timer

    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.sh", "*.service", "*.timer"):
        assert any(line.split()[:1] == [pattern] and "eol=lf" in line for line in attributes.splitlines()), pattern

    from verify_security import _docker_excluded
    lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    patterns = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    for path in ("deploy/sqlite_backup.py", "deploy/backup.sh", "deploy/restore.sh", "deploy/systemd/placement-backup.timer", "verify_backup.py"):
        assert not _docker_excluded(path, patterns), f"{path} is excluded from the build context"
    assert _docker_excluded("placement-20260101T030000Z.db.gz", patterns), "archives would reach the build context"
    print("  [8] ✓ scripts: strict bash, LF, one lock, migrate container with only data + backups mounted; only VACUUM INTO; units: oneshot as deploy, persistent daily timer")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nRunning Phase 1.2 SQLite backup & restore verification suite…\n")

    test_backup_is_verified_compressed_and_identical()
    test_live_writer_is_not_blocked_and_not_leaked()
    test_rotation_keeps_newest_scheduled_only()
    test_failures_leave_nothing_behind()
    test_restore_and_rollback()
    test_restore_refusals_change_nothing()
    test_cli_and_database_url()
    test_scripts_and_units()

    print("\n✅ Phase 1.2 SQLite Backup & Restore Verified Successfully!")


if __name__ == "__main__":
    main()
