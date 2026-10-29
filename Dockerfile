# Stage 1: Builder
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# Install build prerequisites
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Configure Git to trust all mounted directories across host UID boundaries
RUN git config --global --add safe.directory "*"

# Install uv for fast, deterministic dependency resolution
COPY --from=ghcr.io/astral-sh/uv:0.5.26 /uv /uvx /bin/

# Set up virtual environment
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN uv venv /opt/venv

# Copy dependency manifests and source code
COPY pyproject.toml README.md ./
COPY app ./app

# Install project and all dependencies into virtual environment
RUN uv pip install --no-cache-dir .

# Stage 2: Runtime
FROM python:3.12-slim-bookworm AS runtime

LABEL maintainer="DeployPilot DevOps <ops@local>"
LABEL description="Secure GitHub Webhook-Based Docker Deployment Automation"

# Configure Python execution environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    DEPLOYMENT_ROOT="/volume1/docker" \
    CONFIG_PATH="/app/config/repositories.yaml" \
    DATABASE_PATH="/app/data/deployments.db" \
    PORT=8088 \
    HOST=0.0.0.0

WORKDIR /app

# Install runtime system dependencies:
# - git: Required for repository state inspection and non-destructive checkouts
# - curl: Required for container health checks
# - docker-ce-cli & docker-compose-plugin: Required to interact with host Docker daemon
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Configure Git to trust all mounted directories across host UID boundaries
RUN git config --global --add safe.directory "*"

# Create application directories
RUN mkdir -p /app/config /app/data /volume1/docker

# Copy virtual environment from builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application source code and default config
COPY app /app/app
COPY config /app/config.default

# Expose service port
EXPOSE 8088

# Healthcheck probe using liveness endpoint
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://127.0.0.1:${PORT}/api/v1/health || exit 1

# Launch production server via Uvicorn factory
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8088"]

