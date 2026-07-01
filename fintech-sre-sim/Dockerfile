#dockerfile
# building the fintech sre simulation data generator container here.
# it spits out prometheus metrics on :8000/metrics and structured json logs to stdout or a file.

FROM python:3.10-slim
#setting up our working directory
WORKDIR /app
#grabbing just the system dependencies we actually need for the generator

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*


#copying and installing python dependencies first to take advantage of layer caching
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu 
COPY requirements.txt .
RUN pip install --no-cache-dir --default-timeout=100 --retries 10 -r requirements.txt
#copy over the rest of the project files
COPY generators/ ./generators/
COPY config/ ./config/
COPY scenarios/ ./scenarios/

#making a logs directory in case we want to output to a file
RUN mkdir -p /app/logs

# opening up the port for our prometheus metrics endpoint
EXPOSE 8000

# doing a quick health check to verify the metrics endpoint is responding
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# default behavior is steady-state baseline generation.
# you can override this with --mode scenario --scenario <name> or --mode training.
CMD ["python", "-m", "generators.main", "--mode", "steady", "--metrics-port", "8000"]