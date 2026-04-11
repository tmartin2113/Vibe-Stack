# ── Stage 1: Install dependencies ────────────────────────────────────
FROM python:3.13-slim AS deps

WORKDIR /app

# Install pinned production dependencies from lock file for reproducible builds.
# Regenerate with: pip-compile pyproject.toml --extra agents -o requirements-production.lock --strip-extras
COPY requirements-production.lock .
RUN pip install --no-cache-dir -r requirements-production.lock

# ── Stage 2: Runtime image ───────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

# Install only the runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI: needed for resource_discovery to detect Docker via 'docker info'
# (the host Docker socket is bind-mounted into this container)
COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/docker

# Create non-root user for running the application
RUN groupadd -r vibe && useradd -r -g vibe -u 1000 -m -d /home/vibe vibe

# Copy installed Python packages from deps stage.
# Uses BuildKit mount to discover site-packages path dynamically so the build
# doesn't break when Dependabot bumps the Python base image version.
RUN --mount=from=deps,source=/usr/local,target=/deps \
    PYVER=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') && \
    cp -a /deps/lib/python${PYVER}/site-packages/* /usr/local/lib/python${PYVER}/site-packages/
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application code (owned by vibe user)
COPY --chown=vibe:vibe vibe/ vibe/
COPY --chown=vibe:vibe agents/ agents/
COPY --chown=vibe:vibe pyproject.toml .
COPY --chown=vibe:vibe scripts/ scripts/
COPY --chown=vibe:vibe agents/instructions/ /opt/vibe/instructions/

# Install the vibe package itself (non-editable for production)
RUN pip install --no-cache-dir .

# Create data directory for vibe user (includes skills dir for SkillRegistry)
RUN mkdir -p /home/vibe/.vibe/skills && chown -R vibe:vibe /home/vibe/.vibe

# Create empty overrides directory (mounted as volume for dev-time instruction editing)
RUN mkdir -p /opt/vibe/overrides && chown -R vibe:vibe /opt/vibe/overrides

# Switch to non-root user
USER vibe

# Expose health/metrics port
EXPOSE 8080

# Default environment
ENV LOG_LEVEL=INFO
ENV VIBE_BACKEND_HOST=vllm
ENV VIBE_BACKEND_PORT=8000
ENV VIBE_HEALTH_PORT=8080
ENV HOME=/home/vibe

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${VIBE_HEALTH_PORT:-8080}/healthz || exit 1

ENTRYPOINT ["python", "-m", "agents.main"]
CMD ["--orchestrator"]
