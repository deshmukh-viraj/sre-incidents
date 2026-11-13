# fintech sre simulation data generator
# serves prometheus metrics on :8000/metrics and structured json logs to stdout or a file.

FROM python:3.10-slim
# set up working directory
WORKDIR /app
# install system dependencies

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*


# install python dependencies first (layer caching)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu 
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 10 -r requirements.txt
# copy project files
COPY generators/ ./generators/
COPY config/ ./config/
COPY scenarios/ ./scenarios/

# create logs directory
RUN mkdir -p /app/logs

# expose prometheus metrics port
EXPOSE 8000

# health check for metrics endpoint
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# default: steady-state baseline generation.
# override with --mode scenario --scenario <name> or --mode training.
CMD ["python", "-m", "generators.main", "--mode", "steady", "--metrics-port", "8000"]