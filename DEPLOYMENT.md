# Deploying the API and crawler

The backend ships as **one image with two roles**. `docker-entrypoint.sh`
decides which process to start, so the API service and the crawler worker are
the same build with a different start command.

| Role      | Command                        | What it does                                   |
| --------- | ------------------------------ | ---------------------------------------------- |
| `api`     | `./docker-entrypoint.sh api`   | Migrates, then serves FastAPI on `$PORT`       |
| `worker`  | `./docker-entrypoint.sh worker`| Crawls every `CRAWL_INTERVAL_HOURS`            |
| `crawl`   | `./docker-entrypoint.sh crawl` | One crawl, then exits                          |
| `score`   | `./docker-entrypoint.sh score` | Re-runs the scorer (`--llm` for the Claude pass)|
| `seed`    | `./docker-entrypoint.sh seed`  | Loads company data and the first user          |
| `migrate` | `./docker-entrypoint.sh migrate` | `alembic upgrade head`                       |

## Railway

Two services from this one repository, sharing a Postgres plugin.

**1. Postgres.** Add the Postgres plugin to the project first — both services
reference it.

**2. API service.** Config path `railway.json` (the default). Variables:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
JWT_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(48))">
CORS_ORIGINS=https://<your-app>.vercel.app
AUTO_CREATE_TABLES=false
RUN_MIGRATIONS=true
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5
```

`AUTO_CREATE_TABLES=false` matters: if the app runs `create_all` first on an
empty database, the tables exist without an `alembic_version` row and the
baseline migration then fails trying to create them again. Railway injects
`PORT` and healthchecks `/health`; both are already wired up.

Generate a public domain for this service — that URL is what the frontend's
`API_URL` points at.

**3. Crawler worker.** A second service from the same repo, with **Config as
code** set to `railway.worker.json`. Variables:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
AUTO_CREATE_TABLES=false
RUN_MIGRATIONS=false
CRAWL_INTERVAL_HOURS=6
CRAWL_DAYS=7
```

`RUN_MIGRATIONS=false` keeps it from racing the API service's migration on a
simultaneous deploy. Do not generate a domain for it — it serves no HTTP.

**4. Seed once**, from the Railway CLI, after the first deploy:

```bash
railway run --service <api-service> ./docker-entrypoint.sh seed
```

The seeder prints a generated password for `SEED_USER_EMAIL` unless
`SEED_USER_PASSWORD` is set. It is idempotent and never rewrites an existing
user's password, so re-run it whenever the spreadsheets in `data/` change.

**5. First crawl.** The worker crawls immediately on boot rather than waiting a
full interval. Score the results afterwards:

```bash
railway run --service <api-service> ./docker-entrypoint.sh score --llm
```

## Local stack

```bash
cp .env.example .env          # then edit JWT_SECRET, ANTHROPIC_API_KEY
docker compose up -d --build
docker compose run --rm api seed
open http://localhost:8000/docs
```

Postgres is published on **5433**, matching the development `DATABASE_URL`
default and staying clear of a Postgres already running on 5432.

To run the UI in the same stack, use the compose file in the parent directory
instead of this one.

## Company data

The seeders read two spreadsheets from `data/`. They are committed so the image
is self-contained — a container has no access to files outside the build
context. Refresh them by replacing the files and re-running the seeder. Set
`COMPANY_DATA_DIR` to read them from a mounted volume instead.

## Database elsewhere

Any Postgres works — Railway's plugin, Supabase, RDS. Point `DATABASE_URL` at
it and run `./docker-entrypoint.sh migrate` once. On Supabase use the pooled
connection string and keep `DB_POOL_SIZE` small; the pooler counts every
connection against the project limit.
