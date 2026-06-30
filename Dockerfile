FROM python:3.12-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency spec first for layer caching
COPY pyproject.toml ./

# Install package in editable mode (no source needed yet)
RUN pip install --no-cache-dir hatchling && \
    pip install --no-cache-dir \
    anthropic \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    pydantic \
    pydantic-settings \
    python-dotenv \
    tenacity

COPY src/ ./src/

# Non-root user for K8s security context
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
