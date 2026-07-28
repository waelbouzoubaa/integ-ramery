FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY src/ ./src/

WORKDIR /app/src/watcher
CMD ["uv", "run", "--no-sync", "python", "-u", "pdf_watcher.py"]
