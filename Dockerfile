# TARGETARCH and TARGETVARIANT are injected automatically by buildx.
# They must be declared before the first FROM to be usable in FROM instructions.
# For linux/arm/v7:  TARGETARCH=arm, TARGETVARIANT=v7  → selects base-armv7
# For linux/arm/v8:  TARGETARCH=arm, TARGETVARIANT=v8  → selects base-armv8
# For linux/amd64:   TARGETARCH=amd64, TARGETVARIANT=""  → selects base-amd64
# For linux/arm64:   TARGETARCH=arm64, TARGETVARIANT=""  → selects base-arm64
ARG TARGETARCH
ARG TARGETVARIANT

# Base images per platform — all targets use Debian-slim (not Alpine) to avoid
# the musl libc TLS fingerprint that Tesla rejects on long-running token
# refresh (see RELEASE.md v0.15.13 and pypowerwall#344). This matches the
# base image strategy used by the pypowerwall proxy's own Dockerfile.
FROM python:3.12-slim-bookworm AS base-amd64
FROM python:3.12-slim-bookworm AS base-arm64
FROM python:3.12-slim-bookworm AS base-armv7
FROM python:3.12-slim-bookworm AS base-armv8
FROM base-${TARGETARCH}${TARGETVARIANT}

WORKDIR /app

# Install build dependencies, pip packages, then clean up.
# wget and curl are kept as runtime dependencies for health checks — the
# Dockerfile HEALTHCHECK uses wget, but Powerwall-Dashboard's docker-compose
# overrides the healthcheck with curl. tini is kept as the PID 1 entrypoint
# (see ENTRYPOINT below) for correct signal forwarding and zombie reaping.
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        make \
        automake \
        autoconf \
        libtool \
        libffi-dev \
        curl \
        wget \
        tini && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc python3-dev make automake autoconf libtool libffi-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy application
COPY app/ ./app/

# Persistent data mount point for the time-series SQLite store
# (PW_TIMESERIES_PATH defaults to /data/timeseries.db; if set to a
# directory it will be resolved to <dir>/timeseries.db). Mount a volume
# here to keep daily energy history across container rebuilds:
#   docker run ... -v pws-data:/data ...
RUN mkdir -p /data
VOLUME /data

# Expose port
EXPOSE 8675
# HTTPS when PW_HTTPS=yes (PW_HTTPS_PORT)
EXPOSE 8775

# Health check - use /health which always returns HTTP 200 (gateway status is in
# the JSON body). This makes it safe as a process-level liveness check.
# start_period gives the server time to establish its first gateway connection
# before Docker starts counting retries.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD wget --spider -q http://localhost:8675/health || exit 1

# Run under tini as PID 1 so signals (e.g. SIGTERM) are forwarded correctly to
# uvicorn for graceful shutdown, and any orphaned child processes are reaped.
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8675"]
