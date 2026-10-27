#!/usr/bin/env bash
# Placement Engine — restore the SQLite database from a backup (Phase 1.2).
# Procedures and when to use each command: deploy/BACKUP.md.
#
#   deploy/restore.sh --list                          # archives, kept pre-restore sets
#   deploy/restore.sh --verify <archive>              # decompress + integrity check only
#   deploy/restore.sh <archive> [--yes]               # replace the live database
#   deploy/restore.sh --rollback <pre-restore-dir> [--yes]   # undo a restore
#
# <archive> is a file name from --list (placement-<UTC>[-tag].db.gz), never a path.
# A restore verifies the archive first, stops the api and worker, moves the current
# database and its -wal/-shm files into data/pre-restore-<UTC>/, puts the archive's
# database in place and starts the stack again (migrate runs first, so an older
# backup is upgraded to the current schema). Nothing is ever deleted.
#
# Environment: PLACEMENT_ROOT (default /srv/placement). Run as the deploy user.
set -Eeuo pipefail
umask 077

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PLACEMENT_ROOT=${PLACEMENT_ROOT:-/srv/placement}
BACKUP_DIR=$PLACEMENT_ROOT/backups
HELPER=$REPO_DIR/deploy/sqlite_backup.py
IMAGE=placement-engine:local
MOUNTED=/var/lib/placement/backups

die() { echo "restore.sh: $*" >&2; exit 1; }
usage() { die "usage: restore.sh --list | --verify ARCHIVE | ARCHIVE [--yes] | --rollback DIR [--yes]"; }
compose() { docker compose -f "$REPO_DIR/docker-compose.yml" "$@"; }

# helper <archives mount mode: rw | ro | none> <sqlite_backup.py arguments...>
helper() {
  local mode=$1
  shift
  local mount=()
  [[ $mode == none ]] || mount=(--volume "$BACKUP_DIR:$MOUNTED:$mode")
  compose run --rm --no-deps -T "${mount[@]}" migrate python - "$@" <"$HELPER"
}

confirm() {
  [[ $yes == 1 ]] && return 0
  [[ -t 0 ]] || die "not a terminal: pass --yes to confirm"
  local answer
  read -r -p "$1 Type RESTORE to continue: " answer
  [[ $answer == RESTORE ]] || die "cancelled; nothing was changed"
}

stop_app() {
  echo "Stopping api and worker…"
  compose stop worker api
  [[ -z $(compose ps --status running --quiet worker api) ]] || die "api or worker is still running; nothing was changed"
}

start_app() {
  echo "Starting the stack (migrate → api, worker)…"
  compose up -d
  compose ps
}

failed() {
  die "$1 failed. The api and worker are STOPPED and the database is as the message above says.
Check with: deploy/restore.sh --list — then start again with: docker compose up -d"
}

action=restore target= yes=0
while (($#)); do
  case $1 in
    --list) action=list ;;
    --verify) action=verify; target=${2:-}; shift ;;
    --rollback) action=rollback; target=${2:-}; shift ;;
    --yes) yes=1 ;;
    -*) usage ;;
    *) [[ -z $target ]] || usage; target=$1 ;;
  esac
  shift
done
[[ $action == list || -n $target ]] || usage

[[ -d $PLACEMENT_ROOT/data ]] || die "missing $PLACEMENT_ROOT/data"
[[ -d $BACKUP_DIR ]] || die "missing $BACKUP_DIR (deploy/BACKUP.md)"
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not built — run: docker compose build"

exec 9<"$HELPER"
flock -n 9 || die "a backup or restore is running"

case $action in
  list)
    helper ro list --archives "$MOUNTED"
    ;;
  verify)
    helper rw verify "$target" --workdir "$MOUNTED"
    ;;
  restore)
    helper rw verify "$target" --workdir "$MOUNTED"
    confirm "This replaces the live database with $target (the current one is kept)."
    stop_app
    helper ro restore "$target" --archives "$MOUNTED" || failed "Restore"
    start_app
    echo "To undo: deploy/restore.sh --rollback <the pre-restore directory printed above>"
    ;;
  rollback)
    confirm "This puts data/$target back as the live database (the current one is kept)."
    stop_app
    helper none rollback "$target" || failed "Rollback"
    start_app
    ;;
esac
