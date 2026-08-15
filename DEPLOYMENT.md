# Deploying the crawler

This service crawls World Bank and Asian Development Bank procurement notices,
links the ones that advertise the same opportunity, and scores them against
AMANA's capability profile. It owns no schema and knows nothing about users —
all of that lives in the `tender-intelligence` app, which reads the same
database.

It runs a full cycle **every 12 hours**, and answers a trigger endpoint so the
app's dashboard button can ask for one now.

`docker-entrypoint.sh` picks the role:

| Role     | Command                          | What it does                                                 |
| -------- | -------------------------------- | ------------------------------------------------------------ |
| `serve`  | `./docker-entrypoint.sh serve`   | Cycle every `CRAWL_INTERVAL_HOURS` **and** serve `POST /crawl` |
| `cron`   | `./docker-entrypoint.sh cron`    | One full cycle — crawl, score, assess — then exits            |
| `worker` | `./docker-entrypoint.sh worker`  | The same cycle on an interval, with no trigger endpoint       |
| `crawl`  | `./docker-entrypoint.sh crawl`   | Crawl only, then exits                                        |
| `score`  | `./docker-entrypoint.sh score`   | Scoring only (`--llm` for the Claude pass)                    |

`serve` is the default and the one to deploy: it is the only role that can
answer the dashboard's "Start crawling" button, because a platform cron job has
no process between runs to answer anything. `cron` remains the cheaper choice if
you are willing to give that button up — see
[Batch alternative](#batch-alternative).

## The trigger endpoint

`serve` listens on `PORT` (8080 by default):

| Endpoint       | Auth  | Answers                                                        |
| -------------- | ----- | -------------------------------------------------------------- |
| `GET /health`  | none  | `{"status":"ok"}` — liveness only, deliberately says nothing else |
| `GET /status`  | token | Whether a cycle is running, what started it, how the last ended |
| `POST /crawl`  | token | `202` when a cycle starts, `409` when one is already running    |

Authentication is a shared secret in `CRAWLER_TRIGGER_TOKEN`, sent as
`X-Trigger-Token` or `Authorization: Bearer`. **The server refuses to start
without one** — an open endpoint would let anything that can reach the service
spend an LLM budget. Set the same value on the app, and keep this service off
the public internet; only the app should be able to reach it.

Only one cycle runs at a time, whichever asked for it. A trigger that arrives
mid-crawl is refused with `409` and the running job's elapsed time rather than
queued: a second pass over the same feeds would find the same notices. The app
turns that into the "a crawl is already running" message on the dashboard.

```bash
curl -X POST http://localhost:8080/crawl -H "X-Trigger-Token: $CRAWLER_TRIGGER_TOKEN"
curl      http://localhost:8080/status -H "X-Trigger-Token: $CRAWLER_TRIGGER_TOKEN"
```

## Before the first run

The schema and the company data are the app's responsibility, not this
service's. From the `tender-intelligence` repo, against the same database:

```bash
npm run db:migrate    # create the schema
npm run seed          # load the capability profile
```

The scoring stage matches notices against that profile and refuses to run
without one, so seeding has to happen first. Crawling works regardless.

## Railway

One service, sharing the app's Postgres.

**1. Deploy this repo** as a service in the same project as the database, using
`railway.json` (the default config path).

**2. Variables:**

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
CRAWL_DAYS=7
CRAWL_ROWS=500
CRAWL_SOURCE=all
CRAWL_INTERVAL_HOURS=12
CRAWLER_TRIGGER_TOKEN=<openssl rand -hex 32>
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5
LLM_LIMIT=300
LLM_CONCURRENCY=4
```

Without `ANTHROPIC_API_KEY` the cycle still crawls and heuristically scores — it
skips the assessment stage rather than failing.

**3. Point the app at it.** On the `tender-intelligence` service, set

```
CRAWLER_URL=http://<this-service>.railway.internal:8080
CRAWLER_TRIGGER_TOKEN=<the same value as above>
```

The two tokens must match exactly, or the dashboard button reports the crawler
as offline.

**4. Do not generate a public domain.** The trigger endpoint is for the app
only, and Railway's internal network is how it should be reached. Railway sets
`PORT` itself; `server.py` reads it.

`railway.json` runs `serve` with `restartPolicyType: ALWAYS` and a `/health`
check. Keep it at **one replica**: the guard that stops two crawls overlapping
lives in the process, so a second replica would run its own schedule.

### Batch alternative

To trade the dashboard button for a container that only bills while it works,
set the service's config path to `railway.worker.json` and attach a cron
schedule (Settings → Cron Schedule, `0 */12 * * *` for the same cadence). It
runs `cron` with `restartPolicyType: NEVER`, which is load-bearing: the job is
*supposed* to exit, and any restart policy would relaunch it in a loop. With no
process between runs, `POST /crawl` has nothing to answer it and the dashboard
shows "Crawler offline".

## Sources

Two feeds, crawled in one pass and stored in one table, each row tagged with the
bank that published it.

| Source      | Where the notices come from                                                                     |
| ----------- | ----------------------------------------------------------------------------------------------- |
| `worldbank` | The procurement-notices API on `search.worldbank.org`                                             |
| `adb`       | The Solr index behind `adb.org/projects/tenders`, plus the consulting notice bodies in ADB's CMS  |

`CRAWL_SOURCE` (or `--source`) narrows a run to one of them. That is for
debugging a single bank without re-walking the other; production wants `all`.

**Backfill cost.** An incremental ADB run is small — roughly 90 notices a week,
of which the consulting ones cost one extra request each for their body. A full
backfill (`--days 0`) is a different proposition: ~38,000 consulting notices at
one detail page apiece, which at the configured delay runs for hours. It is
worth doing once for the history; it is not something to schedule.

**When the ADB crawl finds nothing.** ADB's tender listing is drawn in the
browser from a hosted Solr index, using a read-only token embedded in its own
public page. If that token is rotated the crawl stops with a message saying so.
To recover, open `https://www.adb.org/projects/tenders` in a browser, read the
`Authorization` header of the request the page makes to `searchstax.com`, and
set it as `ADB_SEARCH_TOKEN`. No release is needed.

Note also that ADB's own site sits behind a challenge this service does not try
to pass. It does not need to: the index and the consulting notice bodies are on
hosts that are not challenged. The one thing out of reach is the PDF body of a
goods or works notice, so those rows carry metadata and a link for a person to
open, and no notice text.

## Duplicates

Both banks co-finance work, so a package can be advertised by each of them. The
crawler links those rows rather than merging them: one is canonical and the other
points at it through `duplicate_of_id`. Scoring skips the linked copy, so no
opportunity is read by Claude twice, and the app lists canonical rows by default
with a badge naming the other bank. The "All postings" toggle shows both.

Merging was the alternative and it would have been wrong — each bank sets its own
deadline, reference number and submission channel, and a bid is made against
exactly one of them. A link is also reversible; the pass recomputes from scratch
on every run, so a wrong link costs a filter toggle rather than a re-crawl.

The match is narrow on purpose: an exact fingerprint of title, country and
closing date, and **only where a group spans two banks**. Both restrictions were
tightened against the live table after looser rules merged things that were not
duplicates — separate lots, separate positions, the two halves of a joint
procurement. `crawler/dedup.py` records what was tried and what it got wrong.
Expect this to link rarely; it errs toward missing, because a missed link shows
one extra row while a wrong one hides an opportunity.

## Exit codes

`cron.py` runs its stages independently — a failed crawl still lets the scorer
work through what is already in the table, and a failed assessment does not lose
the crawl. It exits non-zero if **any** stage failed, and names them in the
final log line, so a scheduler's failure alerting stays meaningful.

`serve` reports the same thing through `GET /status`: `last_run.status` is
`failed` for any non-zero exit, and the dashboard turns that into a warning
rather than a silent green finish. The cycle's own log is the service's log, so
a crawl started from the dashboard is read in the same place as a scheduled one.

## Local

```bash
cp .env.example .env
# Set CRAWLER_TRIGGER_TOKEN in .env first -- the server refuses to start without it.
docker compose up -d crawler-server            # the schedule and the trigger endpoint
docker compose --profile cron run --rm crawler # one cycle now, in the foreground
docker compose --profile cron run --rm crawler crawl  # crawl only
```

`crawler-server` publishes 8080 so a `next dev` running outside compose can
reach it at `http://localhost:8080`; set the app's `CRAWLER_URL` to that and
give it the same token.

Postgres is published on **5433**, matching the development `DATABASE_URL`
default and staying clear of a Postgres already running on 5432.

To run the UI in the same stack, use the compose file in the parent directory.

## Database

Any Postgres works — Railway's plugin, Supabase, RDS. Point `DATABASE_URL` at the
same database the app uses. On Supabase use the pooled connection string and keep
`DB_POOL_SIZE` small; the pooler counts every connection against the project
limit, and this job holds connections for the length of a crawl.

This service never migrates. If a table is missing, the fix is `npm run
db:migrate` in the app, not anything here.
