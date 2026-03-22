FROM python:3.12-slim-bookworm

WORKDIR /app

# Install build dependencies, pip packages, then clean up.
# curl is kept as a runtime dependency for the HEALTHCHECK below.
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        make \
        automake \
        autoconf \
        libtool \
        curl && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc python3-dev make automake autoconf libtool && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy application
COPY app/ ./app/

# Expose port
EXPOSE 8675

# Health check - use the /health endpoint which returns 200 for healthy/degraded/unhealthy
# (all indicate the server process is running).  start_period gives the server time to
# establish its first gateway connection before Docker starts counting retries.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -sf http://localhost:8675/health || exit 1

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8675"]
