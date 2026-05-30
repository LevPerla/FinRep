FROM python:3.10-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    FINREP_DASH_HOST=0.0.0.0 \
    FINREP_DASH_PORT=8050 \
    FINREP_DASH_DEBUG=0 \
    FINREP_DASH_HOT_RELOAD=0

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY assets ./assets
COPY src ./src
COPY main.py README.md ./

EXPOSE 8050

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8050/healthz', timeout=3).read()"

CMD ["python", "-m", "src.dashboard.app"]
