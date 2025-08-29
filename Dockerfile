# Simple Sweeper service
FROM python:3.12-slim

WORKDIR /app

# Install runtime deps
COPY Sweeper/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy code
COPY Sweeper/app /app/app

# Environment defaults
ENV SWEEP_INTERVAL_SECONDS=1800 \
    MIN_SWEEP_XMR=0.0001 \
    LOG_LEVEL=INFO

# Run the periodic loop
CMD ["python", "-m", "app.main"]
