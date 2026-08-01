FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev

COPY alembic/ alembic/
COPY alembic.ini docker-entrypoint.sh ./
COPY config/ config/
RUN useradd --create-home tracker \
    && mkdir -p /app/data /app/config \
    && chown -R tracker:tracker /app \
    && chmod +x /app/docker-entrypoint.sh

USER tracker

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["tracker", "serve"]
