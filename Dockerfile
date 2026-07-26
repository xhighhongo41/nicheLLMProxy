FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    NICHELLM_CONFIG_PATH=/app/config/config.json

WORKDIR /app

RUN groupadd --system nichellm \
    && useradd --system --gid nichellm --create-home nichellm

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN mkdir -p /var/log/nichellm \
    && chown -R nichellm:nichellm /var/log/nichellm

USER nichellm

EXPOSE 8000

CMD ["niche-llm-proxy"]
