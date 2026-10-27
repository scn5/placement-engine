#!/usr/bin/env bash
# Placement Engine — verified SQLite backup (Phase 1.2). Procedures: deploy/BACKUP.md.
#
#   deploy/backup.sh                    # scheduled (placement-backup.timer); rotated
#   deploy/backup.sh --tag pre-deploy   # before an upgrade or a risky change; never rotated
#
# Pipes deploy/sqlite_backup.py into a one-off container of the `migrate` service:
# the app user (10001), no network, and only the data and backups directories
# mounted — never the Telegram session or the secrets. The live database is opened
# read-only and copied with VACUUM INTO while the API and worker keep running; the
# copy must pass PRAGMA integrity_check before it is compressed, kept and rotated.
#
# Environment: PLACEMENT_ROOT (default /srv/placement), BACKUP_KEEP (scheduled
# backups to keep, default 14). Run as the deploy user, never with sudo.
set -Eeuo pipefail
umask 077

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PLACEMENT_ROOT=${PLACEMENT_ROOT:-/srv/placement}
BACKUP_DIR=$PLACEMENT_ROOT/backups
BACKUP_KEEP=${BACKUP_KEEP:-14}
HELPER=$REPO_DIR/deploy/sqlite_backup.py
IMAGE=placement-engine:local
MOUNTED=/var/lib/placement/backups

die() { echo "backup.sh: $*" >&2; exit 1; }

tag=()
case $# in
  0) ;;
  2) [[ $1 == --tag ]] || die "usage: backup.sh [--tag NAME]"; tag=(--tag "$2") ;;
  *) die "usage: backup.sh [--tag NAME]" ;;
esac
[[ $BACKUP_KEEP =~ ^[1-9][0-9]*$ ]] || die "BACKUP_KEEP must be a positive integer"
[[ -d $PLACEMENT_ROOT/data ]] || die "missing $PLACEMENT_ROOT/data"
# Docker would create a missing bind source owned by root, unwritable for the app user.
[[ -d $BACKUP_DIR ]] || die "missing $BACKUP_DIR — create it owned by 10001:10001, mode 0700 (deploy/BACKUP.md)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not built — run: docker compose build"

# One backup or restore at a time (restore.sh takes the same lock). Locking a file
# that is only read needs no writable, pre-created lock file.
exec 9<"$HELPER"
flock -n 9 || die "another backup or restore is running"

docker compose -f "$REPO_DIR/docker-compose.yml" run --rm --no-deps -T \
  --volume "$BACKUP_DIR:$MOUNTED" \
  migrate python - backup --dest "$MOUNTED" --keep "$BACKUP_KEEP" "${tag[@]}" \
  <"$HELPER"
