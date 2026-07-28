FROM python:3.12-slim

WORKDIR /app
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY agent-python/pyproject.toml /app/pyproject.toml
COPY agent-python/agent /app/agent

RUN python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" .

USER nobody
EXPOSE 9100
ENTRYPOINT ["prd-agent-agent-worker"]
