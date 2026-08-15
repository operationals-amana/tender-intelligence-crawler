#!/usr/bin/env sh
#
# One image, a few roles. This service crawls and scores; it owns no schema, and
# every read/write API and all the migrations live in the tender-intelligence
# app. The one thing it serves over HTTP is the `serve` role's trigger endpoint.
#
#   serve    schedule every CRAWL_INTERVAL_HOURS *and* serve POST /crawl (default)
#   cron     one full cycle -- crawl, score, assess -- then exit
#   worker   the same cycle on a loop, but with no trigger endpoint
#   crawl    crawl only, then exit
#   score    score and assess only, then exit
#   <other>  executed verbatim, so `docker run ... sh` still works
#
set -e

role="${1:-serve}"
shift 2>/dev/null || true

case "$role" in
    serve)
        echo "==> serving on :${PORT:-8080}; full cycle every ${CRAWL_INTERVAL_HOURS:-12}h"
        exec python server.py "$@"
        ;;
    cron)
        echo "==> one cycle: crawl (last ${CRAWL_DAYS:-7}d), score, assess"
        exec python cron.py "$@"
        ;;
    worker)
        echo "==> looping every ${CRAWL_INTERVAL_HOURS:-12}h"
        exec python run_crawler.py \
            --schedule \
            --days "${CRAWL_DAYS:-7}" \
            --rows "${CRAWL_ROWS:-500}" \
            --interval-hours "${CRAWL_INTERVAL_HOURS:-12}" "$@"
        ;;
    crawl)
        exec python cron.py --crawl-only "$@"
        ;;
    score)
        exec python score_tenders.py "$@"
        ;;
    *)
        exec "$role" "$@"
        ;;
esac
