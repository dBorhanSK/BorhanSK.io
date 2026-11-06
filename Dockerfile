FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/tmp/borhan-sk-data \
    PORT=10000

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

EXPOSE 10000
# One worker is intentional: the runtime owns one shared Chromium process/event loop.
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "180", "--graceful-timeout", "35", "app:app"]
