# ── Stage 1: Install dependencies ────────────────────────────────────
FROM python:3.11-slim AS deps

WORKDIR /app

# Install pinned production dependencies from lock file for reproducible builds.
# Regenerate with: pip-compile pyproject.toml --extra agents -o requirements-production.lock --strip-extras
COPY requirements-production.lock .
RUN pip install --no-cache-dir -r requirements-production.lock

# ── Stage 2: Runtime image ───────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install only the runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for running the application
RUN groupadd -r genesia && useradd -r -g genesia -m -d /home/genesia genesia

# Copy installed Python packages from deps stage
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code (owned by genesia user)
COPY --chown=genesia:genesia genesia/ genesia/
COPY --chown=genesia:genesia agents/ agents/
COPY --chown=genesia:genesia local_prompt_enhancer.py .
COPY --chown=genesia:genesia pyproject.toml .
COPY --chown=genesia:genesia scripts/ scripts/

# Install the genesia package itself
RUN pip install --no-cache-dir -e .

# Create data directory for genesia user
RUN mkdir -p /home/genesia/.genesia && chown -R genesia:genesia /home/genesia/.genesia

# Switch to non-root user
USER genesia

# Expose health/metrics port
EXPOSE 8080

# Default environment
ENV LOG_LEVEL=INFO
ENV GENESIA_BACKEND_HOST=vllm
ENV GENESIA_BACKEND_PORT=8000
ENV GENESIA_HEALTH_PORT=8080
ENV HOME=/home/genesia

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${GENESIA_HEALTH_PORT:-8080}/healthz || exit 1

ENTRYPOINT ["python", "-m", "agents.main"]
CMD ["--heartbeat"]
