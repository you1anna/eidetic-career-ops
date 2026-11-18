#!/usr/bin/env bash
# Nightly backup of the canonical database: an on-disk SQLite copy (3 kept) and an age-encrypted copy in
# /srv/offsite/work-ops (30 kept) for the Mac to pull daily (ops/offsite-weekly.sh). The owner's data is not
# committed to git (decision #28). The code checkout the site runs from is only read here.
# A receipt (data/backups/last-backup.json) tells the dashboard when the last backup finished.
set -euo pipefail
DATA=/srv/work-ops/data
# Leave the failing line for the caretaker's backup card (it cannot read the journal).
trap 'rc=$? line=$LINENO; mkdir -p -m 700 "$DATA/crashes"; printf "{\"at\": \"%s\", \"error\": \"exit %s\", \"where\": \"wo-backup.sh:%s\"}\n" "$(date -u +%FT%TZ)" "$rc" "$line" > "$DATA/crashes/wo-backup.service.json"' ERR
CODE=/srv/work-ops/work-ops
PY=/srv/work-ops/venv/bin/python
DAY=$(date -u +%F)
exec 9>>"$DATA/.writer.lock"; flock 9
mkdir -p "$DATA/backups"
"$PY" "$CODE/tools/wo.py" --db "$DATA/work-ops.db" backup "$DATA/backups/work-ops-$DAY.db"
# Embed a consistent sidecar snapshot before encryption; existing offsite format remains SQLite.
"$PY" "$CODE/tools/backup_collection.py" "$DATA/collection.db" "$DATA/backups/work-ops-$DAY.db"
# Only the nightly copies (work-ops-YYYY-MM-DD.db); pre-ingest and pre-migrate backups keep their own rules.
ls -1t "$DATA/backups"/work-ops-20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].db | tail -n +4 | xargs -r rm -f
OFFSITE=/srv/offsite/work-ops
ARCHIVE="$OFFSITE/work-ops-$(date -u +%Y%m%dT%H%M%SZ).db.age"
age -R "$CODE/ops/backup-recipient.txt" -o "$ARCHIVE.part" "$DATA/backups/work-ops-$DAY.db"
# An interrupted run may leave a partial file; publish only this successful encryption.
mv "$ARCHIVE.part" "$ARCHIVE"
ls -1t "$OFFSITE"/work-ops-*.db.age | tail -n +31 | xargs -r rm -f

# The writer lock above keeps the parser checkpoint stable while SQLite copies the spool.
if [[ -f "$DATA/email/mail.db" ]]; then
  EMAIL_SNAPSHOT=$(mktemp "$DATA/backups/email.XXXXXX.tar.gz")
  trap 'rm -f "$EMAIL_SNAPSHOT"' EXIT
  "$PY" "$CODE/tools/backup_email.py" "$DATA" "$EMAIL_SNAPSHOT"
  EMAIL_ARCHIVE="$OFFSITE/email-$(date -u +%Y%m%dT%H%M%SZ).tar.gz.age"
  age -R "$CODE/ops/backup-recipient.txt" -o "$EMAIL_ARCHIVE.part" "$EMAIL_SNAPSHOT"
  mv "$EMAIL_ARCHIVE.part" "$EMAIL_ARCHIVE"
  rm -f "$EMAIL_SNAPSHOT"
  ls -1t "$OFFSITE"/email-*.tar.gz.age | tail -n +31 | xargs -r rm -f
fi

printf '{"at": "%s", "file": "work-ops-%s.db"}\n' "$(date -u +%FT%TZ)" "$DAY" > "$DATA/backups/last-backup.json.part"
mv "$DATA/backups/last-backup.json.part" "$DATA/backups/last-backup.json"

echo "backup ok: $DATA/backups/work-ops-$DAY.db and an encrypted copy in $OFFSITE"
# Raises a task on the site if the daily ingest is overdue (tools/failure_tasks.py); never fails the backup.
"$PY" "$CODE/tools/wo.py" --actor agent --db "$DATA/work-ops.db" watch --data "$DATA" || echo "watch failed (backup is fine)"
