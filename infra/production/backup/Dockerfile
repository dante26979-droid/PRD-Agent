FROM postgres:16-alpine

ARG APK_REPOSITORY_HOST=dl-cdn.alpinelinux.org
RUN sed -i "s/dl-cdn.alpinelinux.org/${APK_REPOSITORY_HOST}/g" /etc/apk/repositories
RUN apk add --no-cache openssl
COPY infra/production/backup/backup.sh /usr/local/bin/prd-agent-backup
COPY infra/production/backup/restore-backup.sh /usr/local/bin/prd-agent-restore
RUN chmod 0555 /usr/local/bin/prd-agent-backup /usr/local/bin/prd-agent-restore
ENTRYPOINT ["/usr/local/bin/prd-agent-backup"]
