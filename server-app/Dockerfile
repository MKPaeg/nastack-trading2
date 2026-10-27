FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY index.html app.js trade-ui.js live-quotes.js auto-trade-ui.js trade-journal-ui.js reports-ui.js llm-settings-ui.js collection-ui.js data-collection-ui.js recommendations-ui.js screen-help.js styles.css live.css mobile.css manifest.webmanifest service-worker.js ./
RUN groupadd --gid 10001 trading && useradd --uid 10001 --gid trading --no-create-home trading
USER trading
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "nastack_trading.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
