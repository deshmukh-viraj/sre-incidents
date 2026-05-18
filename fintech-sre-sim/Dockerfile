# Dockerfile
# Builds the fintech SRE simulation data generator container.
# Produces: Prometheus metrics on :8000/metrics + structured JSON logs to stdout/file

FROM python:3.10-slim

# System deps — only what's needed for the generator
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy and install Python dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full project
COPY generators/ ./generators/
COPY config/ ./config/

# Create logs directory for file output mode
RUN mkdir -p /app/logs

# Expose Prometheus metrics endpoint
EXPOSE 8000

# Health check — verifies metrics endpoint is responding
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: steady-state baseline generation
# Override with --mode scenario --scenario <name> or --mode training
CMD ["python", "-m", "generators.main", "--mode", "steady", "--metrics-port", "8000"]