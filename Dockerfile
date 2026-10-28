FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY src/ ./src/

WORKDIR /app/src/watcher

COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh
RUN sh /scripts/update.sh || true

ENTRYPOINT [ "/scripts/vault-wrapper.sh" ]
CMD ["uv", "run", "--no-sync", "python", "-u", "pdf_watcher.py"]
