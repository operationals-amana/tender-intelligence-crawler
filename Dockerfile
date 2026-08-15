# syntax=docker/dockerfile:1

# --- builder --------------------------------------------------------------
# Wheels are built in a throwaway stage so the compiler toolchain never reaches
# the runtime image. lxml and psycopg2-binary publish manylinux wheels for
# CPython 3.11, but the toolchain stays here so the build still succeeds if a
# future pin has to compile from source, or the base is not amd64.
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# --- runtime --------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Shared libraries the wheels link against at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY . .

# Scrapy writes its cache and job directories relative to the working
# directory, so /app has to be writable by the unprivileged runtime user.
RUN chmod +x docker-entrypoint.sh \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

# The `serve` role listens here for the dashboard's "Start crawling" trigger and
# schedules the recurring cycle. The batch roles (`cron`, `crawl`, `score`) bind
# nothing and simply exit -- EXPOSE is a declaration, not a requirement, so the
# same image serves both shapes.
EXPOSE 8080

# /health is the only unauthenticated endpoint, and it answers "is the process
# up" rather than "is a crawl healthy" -- a crawl that failed must not fail the
# probe and get the service restarted out from under the next one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT','8080') + '/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["serve"]
