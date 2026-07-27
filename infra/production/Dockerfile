FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system prdagent \
    && useradd --system --gid prdagent --create-home prdagent

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY infra/local ./infra/local
RUN python -m pip install --upgrade pip \
    && python -m pip install '.[api,workflow,production]'

USER prdagent
EXPOSE 8000

CMD ["uvicorn", "prd_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
