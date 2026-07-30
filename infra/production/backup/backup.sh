#!/bin/sh
set -eu

umask 077
interval="${PRD_AGENT_BACKUP_INTERVAL_SECONDS:-900}"
retention_days="${PRD_AGENT_BACKUP_RETENTION_DAYS:-90}"
case "$interval:$retention_days" in
  *[!0-9:]*|:*|*:) echo "backup interval and retention must be positive integers" >&2; exit 2 ;;
esac
if [ "$interval" -lt 60 ] || [ "$retention_days" -lt 1 ]; then
  echo "backup interval must be >= 60 seconds and retention >= 1 day" >&2
  exit 2
fi

work=""
encrypted_tmp=""
cleanup() {
  [ -z "$work" ] || rm -f "$work"
  [ -z "$encrypted_tmp" ] || rm -f "$encrypted_tmp"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

while true; do
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  work="/work/prd-agent-${timestamp}.dump"
  encrypted="/backups/prd-agent-${timestamp}.dump.enc"
  encrypted_tmp="${encrypted}.tmp"
  dsn="$(tr -d '\r\n' < /run/secrets/database_dsn)"

  pg_dump --format=custom --no-owner --no-privileges --file="$work" "$dsn"
  pg_restore --list "$work" >/dev/null
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass file:/run/secrets/backup_encryption_key \
    -in "$work" -out="$encrypted_tmp"
  mv "$encrypted_tmp" "$encrypted"
  encrypted_tmp=""
  sha256sum "$encrypted" > "${encrypted}.sha256"
  date -u +%s > /backups/latest-success
  find /backups -type f -name 'prd-agent-*.dump.enc*' -mtime "+${retention_days}" -delete
  rm -f "$work"
  work=""
  sleep "$interval"
done
