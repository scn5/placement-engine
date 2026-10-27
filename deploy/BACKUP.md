# Backups, restore and rollback (Phase 1.2)

Everything the system knows lives in one SQLite file, `/srv/placement/data/placement.db`
(WAL mode, with `-wal` and `-shm` side files). This page covers how it is backed up,
how to restore it, how to undo a restore, and how to roll back a bad deploy.
VM preparation and first deployment are in `deploy/RUNBOOK.md` (Phase 1.3).

Run every command below as the **deploy user**, from the checkout (`/srv/placement/app`),
unless it says `sudo`.

| File | Role |
|---|---|
| `deploy/backup.sh` | Takes one verified backup (`--tag NAME` for a manual one) |
| `deploy/restore.sh` | `--list`, `--verify`, restore, `--rollback` |
| `deploy/sqlite_backup.py` | The logic both scripts run inside a one-off `migrate` container |
| `deploy/systemd/placement-backup.{service,timer}` | Daily run at 03:00 IST |

## 1. How a backup works

1. `backup.sh` starts a one-off container of the `migrate` service: the app user
   (10001), no network, only `data/` and `backups/` mounted. The Telegram session and
   the secrets are never mounted, so they are never in a backup.
2. The live database is opened **read-only** and copied with `VACUUM INTO`. This is a
   single read transaction: the API and worker keep writing while it runs, the copy
   holds only committed data, and it comes out compacted (free pages dropped).
3. The copy must pass `PRAGMA integrity_check`. It is then gzip-compressed, and the
   archive is decompressed again and compared with the copy byte for byte (SHA-256).
4. Only then is it renamed to `backups/placement-<UTC time>.db.gz`. Rotation runs last
   and keeps the newest **14** scheduled archives (`BACKUP_KEEP`). Archives with a tag
   (`placement-<UTC>-pre-deploy.db.gz`) are never rotated.

If any step fails, the run exits non-zero, no archive is kept and nothing is rotated.
There must be free space for twice the database size.

Never run a plain `VACUUM` on the live database, and never `cp` it: `VACUUM` rewrites the
file under an exclusive lock that blocks the worker, and a copy of a WAL database without
its `-wal` file loses recent writes or is inconsistent. The live file is not compacted
in place; a restore puts back a compacted copy.

**Not in a backup**, keep these elsewhere:

- `secrets/*.env`: keep the values in your password manager. They can be recreated from
  `deploy/templates/`, but the tunnel token and API key must then be rotated.
- `telegram/placement_session.session`: never copied anywhere. After a loss, log in
  again (`docker compose run --rm -it worker python scripts/login_telegram.py`).

## 2. One-time setup

```bash
# Backups directory: owned by the app user, like data/ and telegram/
sudo install -d -o 10001 -g 10001 -m 0700 /srv/placement/backups

# The timer (adjust User= and the paths in the .service if your layout differs)
sudo cp deploy/systemd/placement-backup.service deploy/systemd/placement-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now placement-backup.timer

# First run now, and check it
sudo systemctl start placement-backup
journalctl -u placement-backup -n 20 --no-pager     # expect "backup ok: placement-…"
systemctl list-timers placement-backup.timer        # next run
```

`Persistent=true` means a run missed while the VM was off happens at the next boot.

## 3. Routine checks

| When | Command | Expect |
|---|---|---|
| Weekly | `journalctl -u placement-backup --since -7d --no-pager \| grep -E "backup ok\|ERROR"` | One `backup ok` per day, no `ERROR` |
| Weekly | `systemctl is-failed placement-backup` | `inactive` (not `failed`) |
| Monthly (restore drill) | `deploy/restore.sh --list`, then `deploy/restore.sh --verify <newest archive>` | `verify ok` with sensible row counts |
| Before every deploy | `deploy/backup.sh --tag pre-deploy` | `backup ok: placement-…-pre-deploy.db.gz` |

Nothing alerts you when a backup fails yet; these checks are how you find out.

**Off-VM copies.** Archives on the VM do not survive losing the VM or its boot volume.
Pull one to your own machine now and then (it holds placement data, not the session):

```bash
ssh <vm> 'sudo cat /srv/placement/backups/placement-<UTC>.db.gz' > placement-<UTC>.db.gz
```

## 4. Restore the database from a backup

Use this when the database is damaged, or data was lost or wrongly changed.

1. **Choose the archive.** `deploy/restore.sh --list` shows the archives, newest first.
2. **Check it.** `deploy/restore.sh --verify <archive>` decompresses it, runs the
   integrity check and prints the schema revision and per-table row counts. Nothing
   changes.
3. **Restore.** `deploy/restore.sh <archive>` and type `RESTORE` to confirm. It:
   - verifies the archive again, before stopping anything;
   - stops `api` and `worker` (the app cannot reach the server meanwhile);
   - moves `placement.db` and its `-wal`/`-shm` files, together, into
     `data/pre-restore-<UTC>/` (a WAL file next to a different database would corrupt it);
   - puts the archive's database in place, then runs `docker compose up -d`:
     `migrate` upgrades an older backup to the current schema, then `api` and `worker` start.
4. **Check the result.**
   - `docker compose ps`: `migrate` exited 0; `api` healthy; `worker` running.
   - `curl -s https://<your hostname>/health` → `{"status":"ok"}`; `/status` with the API key.
   - Open the app: events and companies look as of the backup time.
   - `docker compose logs worker | grep -i catch-up`: shows the worker catching up.

**What a restore loses.** Telegram data newer than the backup comes back on its own:
at boot the worker's catch-up re-reads every tracked chat from its saved position, up to
`CATCHUP_MAX_AGE_HOURS` (72 h) back. It runs those messages through the LLM again. What
does **not** come back is what you did in the app after the backup: status changes,
resolved reviews, settings. Redo those by hand.

If step 3 fails, the script says so and leaves `api` and `worker` **stopped**. Read the
message: a failure before "restore ok" changed nothing, so `docker compose up -d` brings
back the old state.

## 5. Undo a restore

The database the restore replaced is in `data/pre-restore-<UTC>/` (shown by
`deploy/restore.sh --list` and printed by the restore).

```bash
deploy/restore.sh --rollback pre-restore-<UTC>
```

It stops `api` and `worker`, moves the restored database into `data/rolled-back-<UTC>/`,
puts the `pre-restore` files back exactly as they were (including their `-wal`), and
starts the stack.

Nothing is deleted automatically. Once you are sure, remove what you no longer need:
`sudo rm -r /srv/placement/data/pre-restore-<UTC> /srv/placement/data/rolled-back-<UTC>`.
Tagged archives are also kept until you delete them:
`sudo rm /srv/placement/backups/placement-<UTC>-<tag>.db.gz`.

## 6. Roll back a bad deploy

Every deploy starts with `deploy/backup.sh --tag pre-deploy` (§3). If the new version
misbehaves:

1. **Go back to the previous code** and rebuild:
   ```bash
   git log --oneline -5                  # find the previous commit
   git checkout <previous commit>
   docker compose up -d --build
   ```
2. **If the bad version had no new migration**, that's it: `migrate` sees the same head
   revision and the old code runs on the current data, with nothing lost.
3. **If it added a migration**, `migrate` fails with "Can't locate revision". The newer
   schema is unknown to the old code, and `api` and `worker` do not start (they wait on
   `migrate`). Restore the pre-deploy backup:
   ```bash
   deploy/restore.sh --list              # placement-<UTC>-pre-deploy.db.gz
   deploy/restore.sh placement-<UTC>-pre-deploy.db.gz
   ```
   This loses app changes since the deploy (§4); Telegram data is caught up again.

Migrations are never downgraded in production (`alembic downgrade` is not used): going
back always means restoring the pre-deploy backup.

## 7. Rebuild on a new VM

1. Prepare the VM and checkout as in `deploy/RUNBOOK.md`, including `data/` and
   `backups/` (both `10001:10001`, `0700`). Do not start the stack yet.
2. Copy an archive in, keeping the app user as owner:
   ```bash
   scp placement-<UTC>.db.gz <new vm>:/tmp/
   sudo install -o 10001 -g 10001 -m 0600 /tmp/placement-<UTC>.db.gz /srv/placement/backups/
   rm /tmp/placement-<UTC>.db.gz
   ```
3. Recreate the secrets, `docker compose build`, and log in to Telegram again as the
   RUNBOOK describes. The login runs `migrate` first, which creates an empty database.
4. `deploy/restore.sh placement-<UTC>.db.gz`. The empty database is moved aside into
   `data/pre-restore-<UTC>/` (delete it afterwards), the archive's database goes in place
   and the stack starts. Continue with the RUNBOOK's verification.
