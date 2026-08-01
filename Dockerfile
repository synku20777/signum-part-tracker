# Use the official Microsoft Playwright image as required by the adapter
# Pinned to match the playwright version in pyproject.toml
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install uv for dependency management
RUN pip install --no-cache-dir uv

# Copy uv config and install dependencies (frozen)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Copy the rest of the application
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .
COPY config/ config/

# Create volume mount points and set permissions
# Playwright image uses `pwuser` as the default non-root user
RUN mkdir -p /app/data && \
    chown -R pwuser:pwuser /app/data /app/config

# Run as non-root user
USER pwuser

# Healthcheck to verify the FastAPI app is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

EXPOSE 8000

# Default command starts the FastAPI app with the embedded scheduler
ENTRYPOINT ["/app/.venv/bin/tracker"]
CMD ["serve"]
