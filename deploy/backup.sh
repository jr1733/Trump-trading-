#!/bin/sh
# Nightly pg_dump with N-day retention, as a long-running container rather than
# a host cron job.
#
# Why not cron: the dump has to be taken with a pg_dump whose version matches
# the server's, and the database here is pinned to pgvector/pgvector:pg16. Using
# the same image for the backup means the two can never drift apart, and means
# the host needs no postgres client installed at all.
#
# Format is custom (-Fc): compressed, and restorable table-by-table with
# pg_restore, which matters when what you actually want back is one table rather
# than the whole database.
#
# The retention delete runs only after a successful dump. A failing dump must
# never be able to age out the backups that still work -- that turns one bad
# night into total data loss.

set -eu

: "${DATABASE_HOST:=db}"
: "${DATABASE_USER:=postgres}"
: "${DATABASE_NAME:=trumpmarket}"
: "${BACKUP_RETENTION_DAYS:=7}"
: "${BACKUP_HOUR:=3}"          # local hour to run, 0-23
BACKUP_DIR=/backups

log() { echo "[backup] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

dump_once() {
    mkdir -p "$BACKUP_DIR"
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    target="$BACKUP_DIR/trumpmarket-$stamp.dump"

    log "dumping to $target"
    # Write to .partial first and rename on success, so an interrupted dump can
    # never be mistaken for a complete one by the restore command.
    if pg_dump -h "$DATABASE_HOST" -U "$DATABASE_USER" -d "$DATABASE_NAME" \
            -Fc -f "$target.partial"; then
        mv "$target.partial" "$target"
        log "ok: $(du -h "$target" | cut -f1)"
    else
        rm -f "$target.partial"
        log "FAILED -- keeping all existing backups"
        return 1
    fi

    # Retention, only on success.
    deleted=$(find "$BACKUP_DIR" -name 'trumpmarket-*.dump' -type f \
        -mtime "+$BACKUP_RETENTION_DAYS" -print -delete | wc -l)
    log "retention: removed $deleted backup(s) older than ${BACKUP_RETENTION_DAYS}d"
    log "on disk: $(find "$BACKUP_DIR" -name 'trumpmarket-*.dump' | wc -l) backup(s)"
}

# `backup.sh once` takes a single dump and exits -- used by `make backup-now`
# and by the restore test.
if [ "${1:-}" = "once" ]; then
    dump_once
    exit $?
fi

log "started; will dump daily at ${BACKUP_HOUR}:00 UTC, keeping ${BACKUP_RETENTION_DAYS} days"
while :; do
    now_hour=$(date -u +%H)
    # Strip the leading zero so 08 is not read as invalid octal.
    if [ "$((10#$now_hour))" -eq "$((10#$BACKUP_HOUR))" ]; then
        dump_once || log "continuing despite failure"
        sleep 3600          # past the trigger hour
    fi
    sleep 600
done
