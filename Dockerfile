FROM python:3.11-slim

WORKDIR /app

# Install build dependencies, pip packages, then clean up
COPY requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        python3-dev \
        make \
        automake \
        autoconf \
        libtool && \
    pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc python3-dev make automake autoconf libtool && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy application
COPY app/ ./app/

# Expose port
EXPOSE 8675

# Run the application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8675"]
