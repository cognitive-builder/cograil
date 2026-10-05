# Deploying to Cloud Run

Cograil ships as one container image. Cloud Run runs it and scales it to zero when nobody is using it. The database is Postgres on Neon. A tag that starts with `v` deploys through `.github/workflows/deploy.yml`.

Think of Cloud Run as a shop that opens only when a customer walks in. The first customer waits while the lights come on (the cold start). Later customers do not.

## The Image

The `Dockerfile` has two stages. The first installs Cograil and its dependencies into a virtualenv. The second copies only that virtualenv, the Workspace, and the Alembic migrations onto a clean `python:3.12-slim`. It runs as an unprivileged user (`cograil`) and listens on `$PORT` (Cloud Run sets it; the fallback is 8080). It trusts forwarded headers (`X-Forwarded-For`) only from the addresses in `FORWARDED_ALLOW_IPS`, which defaults to loopback, so a directly published port cannot spoof them. The Cloud Run deploy sets `FORWARDED_ALLOW_IPS=*`, which is safe only behind Cloud Run. Do not publish the container port directly with that setting.

| Build argument | Default | Meaning |
| --- | --- | --- |
| `WORKSPACE` | `workspaces/example-smb` | The Workspace folder baked into the image at `/workspace`. Point it at your own pack. |
| `EXTRAS` | `oidc` | The optional dependency groups to install, comma separated (`oidc`, `slack`, `mcp`). |

```bash
docker build --build-arg WORKSPACE=workspaces/my-pack -t cograil .
```

Measured on the default build:

| Measure | Value |
| --- | --- |
| Compressed (what Artifact Registry stores and Cloud Run pulls) | about 101 MB |
| Unpacked (what `docker images` can show) | about 338 MB |

The deploy workflow fails the build if the compressed image reaches 300 MB. The unpacked size is above 300 MB because 66 MB of it is Python bytecode. Leaving the bytecode out brings the unpacked image to about 291 MB but doubles the cold start (see below), so the image keeps it.

## Configuration

The image holds no secrets and no database address. Everything comes from the Cloud Run environment. The names are in `docs/api.md` and `docs/auth.md`.

| Variable | Where it lives in Cloud Run |
| --- | --- |
| `DATABASE_URL` | Secret Manager secret `cograil-database-url`. It holds the `cograil_app` URL, not the owner URL (see "Database Roles"). |
| `ANTHROPIC_API_KEY` | Secret Manager secret `cograil-anthropic-api-key` |
| `COGRAIL_OIDC_CLIENT_SECRET` | Secret Manager secret `cograil-oidc-client-secret` |
| `COGRAIL_SESSION_SECRET` | Secret Manager secret `cograil-session-secret` |
| `COGRAIL_AUTH`, `COGRAIL_OIDC_*` (not secret) | Plain environment variables on the service, set once |
| `COGRAIL_CHAT_RATE_LIMIT` (optional, not secret) | Plain environment variable on the service. See "Chat Rate Limit". |

The workflow maps the four secrets on every deploy. It never reads their values, and GitHub never holds them. Plain variables you set on the service stay in place across deploys.

### Chat Rate Limit

`COGRAIL_CHAT_RATE_LIMIT` limits how often one Principal may call `POST /chat`. The form is `<requests>/<seconds>`. The default is `20/60`: 20 requests in any 60 seconds. `off` turns the limit off. Both numbers must be positive whole numbers. Any other value stops the service at start (`LimitNotConfigured`).

The count is held in the process. Cloud Run may run several instances, and a service may run several workers, and each one counts on its own. The effective limit is the limit times the number of processes. Slack messages are not rate limited. See "Limits" in `docs/api.md`.

### Neon

Create a Neon project and a database. Neon gives you a connection string for the owner, the role that created the database. Use the `asyncpg` driver and `ssl=require`:

```
postgresql+asyncpg://OWNER:PASSWORD@ep-example-123456.REGION.aws.neon.tech/DBNAME?ssl=require
```

asyncpg does not accept `sslmode=` or `channel_binding=`; leave them out. Neon suspends an idle database as well, so the first query after a quiet spell can add a short wait on top of the Cloud Run cold start.

Set this up in three steps.

1. Apply the migrations with the owner connection string, from a machine that can reach the database, with the image or a checkout. Alembic reads it from `MIGRATIONS_DATABASE_URL`. This also installs the `vector` extension and creates the `cograil_app` role. The owner needs `CREATEROLE` (or to be a superuser) for that.

   ```bash
   docker run --rm -e MIGRATIONS_DATABASE_URL="postgresql+asyncpg://OWNER:...@.../DBNAME?ssl=require" cograil alembic upgrade head
   ```

2. Set the `cograil_app` password once, as the owner, with `psql` or the Neon SQL editor. Nobody can log in as `cograil_app` until you do. The password is a secret. Never commit it.

   ```sql
   ALTER ROLE cograil_app PASSWORD '...';
   ```

3. Store the `cograil_app` URL in `cograil-database-url`. Do not store the owner URL there.

   ```
   postgresql+asyncpg://cograil_app:PASSWORD@ep-example-123456.REGION.aws.neon.tech/DBNAME?ssl=require
   ```

### Database Roles

Cograil uses two Postgres roles. Each has its own URL.

| Role | Connects as it | Can do |
| --- | --- | --- |
| Owner (the role that created the database, such as Neon's default role) | Migrations, through `MIGRATIONS_DATABASE_URL` | Create and alter tables. Never given to the running service. |
| `cograil_app` | The application, through `DATABASE_URL`: the API server, the `cograil` CLI and `cograil knowledge sync` | Only the grants below. On `audit_events` it can INSERT and SELECT, nothing else. |

The `audit_events` table has triggers that reject UPDATE, DELETE and TRUNCATE. They stop application code, but not the owner, who can run `ALTER TABLE audit_events DISABLE TRIGGER`. The audit trail should not rest on triggers alone, so the application runs as a role that owns nothing and cannot do that.

| Table | What `cograil_app` can do |
| --- | --- |
| `runs` | SELECT, INSERT, UPDATE |
| `tool_calls` | SELECT, INSERT |
| `approvals` | SELECT, INSERT, UPDATE |
| `audit_events` | SELECT, INSERT |
| `chunks` | SELECT, INSERT, UPDATE, DELETE |

It can also use the sequences `tool_calls_id_seq` and `audit_events_id_seq`, and the current schema.

If `MIGRATIONS_DATABASE_URL` is not set, Alembic falls back to `DATABASE_URL`. That is fine for local development with `docker compose` and for CI, where both name the owner `cograil`. When `DATABASE_URL` names `cograil_app`, you must set `MIGRATIONS_DATABASE_URL`, because `cograil_app` cannot create or alter tables.

Roles belong to the whole Postgres cluster, not to one database. The downgrade of the migration revokes the grants, and drops the role only if no other database still grants it anything. A later migration that adds a table must also `GRANT` `cograil_app` what the application needs on it.

## One-Time Setup

1. Create an Artifact Registry Docker repository named `cograil` in your region.
2. Create the four secrets above in Secret Manager.
3. Create a service account for deploys with `roles/run.admin`, `roles/artifactregistry.writer` and `roles/iam.serviceAccountUser` (on the runtime service account). Give the Cloud Run runtime service account `roles/secretmanager.secretAccessor` on the four secrets.
4. Create a Workload Identity pool and an OIDC provider for GitHub, restrict it with an attribute condition that names the repository, the deploy workflow and a version tag (`assertion.repository == 'cognitive-builder/cograil' && assertion.workflow_ref.startsWith('cognitive-builder/cograil/.github/workflows/deploy.yml@refs/tags/v')`; a manual run of the deploy must then start from a tag too). Do not trust the whole repository: other workflows here, including the ones that run a model on text from issues, also ask GitHub for an identity token, and a repository-only condition would let any of them act as the deploy. Then let the deploy service account be impersonated by that pool's identities. There are no JSON keys at any point: GitHub proves who it is with a short-lived token and Google swaps it for a short-lived credential.
5. Add four repository secrets: `GCP_PROJECT`, `GCP_REGION`, `GCP_WORKLOAD_IDENTITY_PROVIDER` (the provider's full resource name) and `GCP_SERVICE_ACCOUNT` (the deploy account's email).
6. Create the service once, before the first tag, with a placeholder image and the plain variables. Cograil refuses to start without `COGRAIL_AUTH`, so the first real revision needs them already in place:

   ```bash
   gcloud run deploy cograil --region REGION \
     --image us-docker.pkg.dev/cloudrun/container/hello \
     --set-env-vars COGRAIL_AUTH=oidc,COGRAIL_OIDC_METADATA_URL=...,COGRAIL_OIDC_CLIENT_ID=...,COGRAIL_OIDC_ALLOWED_DOMAINS=...,COGRAIL_OIDC_REDIRECT_URL=https://SERVICE_URL/auth/callback
   ```

   The service URL is only known after the first deploy, so deploy once, read the URL, then run `gcloud run services update cograil --update-env-vars COGRAIL_OIDC_REDIRECT_URL=...`.

The workflow deploys with `--allow-unauthenticated`. Browsers have to reach the sign-in page, and the app refuses every other request without a session. Never set `COGRAIL_AUTH=dev` on a deployed service: with `COGRAIL_ENV=production`, which the workflow sets, the service refuses to start in that mode.

## Scale to Zero and the Cold Start

The workflow deploys with `--min-instances=0`, so Cloud Run removes the last instance after a period with no requests and bills nothing while it is gone. The next request starts a new instance.

Local measurement: the time from `docker run` to the first `200` on `/health`, with the default image, three runs on a build machine with a warm image cache:

| Image | Time to first `/health` |
| --- | --- |
| Default (bytecode kept) | 3.1 to 3.6 s |
| Without bytecode | 6.6 to 7.3 s |

Most of the time is Python importing LangGraph, the Anthropic SDK and SQLAlchemy. Cloud Run adds the image pull on a cold node and the time to route the request, so expect more than the local number on a real cold start.

Every deploy ends with a step that sends the first request to the new revision (a cold start by definition) and writes the time to the run summary. Read the "Cold start" line there to see the current figure on Cloud Run. To check scale to zero by hand, wait about 15 minutes after the last request, then run:

```bash
gcloud run services describe cograil --region REGION --format='value(status.url)'
curl -s -o /dev/null -w '%{time_total}s\n' "$URL/health"
```

and confirm in the Cloud Run console (metric "Container instance count") that the count fell to zero before the request.

If a cold start is too slow for your users, set `--min-instances=1` on the service. It costs a running instance all month.

### Background work needs a running instance

Two things run inside the service process, on a timer: the **approval sweep** (it expires and escalates overdue Approvals, once a minute) and the **scheduler** for a Colleague's Schedules (docs/architecture.md). Cloud Run gives a process CPU only while it serves a request, and with `--min-instances=0` there is no process at all while the service is idle. So with the workflow as shipped, a Schedule does not fire and an overdue Approval is not escalated until a request wakes the service (the startup sweep then catches up what fell due, and a Schedule's missed tick is dropped after a minute).

If you use Schedules, or you must not leave an Approval waiting after its deadline, deploy with `--min-instances=1 --no-cpu-throttling`. It costs a running instance all month; the decision is tracked in #289.
