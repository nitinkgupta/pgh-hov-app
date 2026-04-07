FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY templates/ templates/

EXPOSE 5050

# Single worker: the background analysis thread uses global
# state (status_cache), so we need exactly 1 worker process.
# 4 threads handle concurrent HTTP requests.
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "1", "--threads", "4", "--timeout", "300", "app:app"]
