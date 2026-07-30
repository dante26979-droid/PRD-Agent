FROM nginx:1.29-alpine

COPY infra/production/ingress/10-secrets.envsh /docker-entrypoint.d/10-secrets.envsh
COPY infra/production/ingress/default.conf.template /etc/nginx/templates/default.conf.template
RUN chmod 0555 /docker-entrypoint.d/10-secrets.envsh
