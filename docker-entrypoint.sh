#!/usr/bin/env sh
#
# One image, several roles. The API service, the crawler worker and the one-off
# maintenance commands all enter here so that schema migrations are applied in
# exactly one place and every environment starts the processes the same way.
#
#   api      migrate, then serve the FastAPI app on $PORT (default 8000)
#   worker   run the crawler forever on CRAWL_INTERVAL_HOURS
#   crawl    run a single crawl and exit
#   score    heuristic-score every tender and exit
#   migrate  alembic upgrade head and exit
#   seed     load the AMANA company data and the first user, then exit
#   <other>  executed verbatim, so `docker run ... bash` still works
#
set -e

migrate() {
    # Deployments let alembic own the schema; AUTO_CREATE_TABLES=false stops the
    # app from racing it with create_all on an empty database.
    if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
        echo "==> alembic upgrade head"
        alembic upgrade head
    else
        echo "==> RUN_MIGRATIONS=${RUN_MIGRATIONS}, skipping migrations"
    fi
}

role="${1:-api}"
shift 2>/dev/null || true

case "$role" in
    api)
        migrate
        echo "==> uvicorn on port ${PORT:-8000}"
        exec uvicorn api.main:app --host 0.0.0.0 --port "${PORT:-8000}" "$@"
        ;;
    worker)
        migrate
        echo "==> scheduled crawler: every ${CRAWL_INTERVAL_HOURS:-6}h, last ${CRAWL_DAYS:-7}d"
        exec python run_crawler.py \
            --schedule \
            --days "${CRAWL_DAYS:-7}" \
            --rows "${CRAWL_ROWS:-500}" \
            --interval-hours "${CRAWL_INTERVAL_HOURS:-6}" "$@"
        ;;
    crawl)
        exec python run_crawler.py --days "${CRAWL_DAYS:-7}" --rows "${CRAWL_ROWS:-500}" "$@"
        ;;
    score)
        exec python score_tenders.py "$@"
        ;;
    migrate)
        echo "==> alembic upgrade head"
        exec alembic upgrade head
        ;;
    seed)
        migrate
        exec python -m api.seed.run_all "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac
