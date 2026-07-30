#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: restore-backup.sh /backups/prd-agent-<timestamp>.dump.enc" >&2
  exit 2
fi
if [ "${PRD_AGENT_RESTORE_CONFIRM:-}" != "RESTORE_INTO_ISOLATED_DATABASE" ]; then
  echo "set PRD_AGENT_RESTORE_CONFIRM=RESTORE_INTO_ISOLATED_DATABASE" >&2
  exit 2
fi

encrypted="$1"
test -f "$encrypted"
sha256sum -c "${encrypted}.sha256"
temporary="$(mktemp /tmp/prd-agent-restore.XXXXXX.dump)"
trap 'rm -f "$temporary"' EXIT
openssl enc -d -aes-256-cbc -pbkdf2 \
  -pass file:/run/secrets/backup_encryption_key \
  -in "$encrypted" -out="$temporary"
pg_restore --list "$temporary" >/dev/null
target_dsn="$(tr -d '\r\n' < /run/secrets/restore_database_dsn)"
pg_restore --exit-on-error --no-owner --no-privileges --dbname="$target_dsn" "$temporary"
