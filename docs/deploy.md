# Deploying to Cloud Run

Cograil ships as one container image. Cloud Run runs it and scales it to zero when nobody is using it. The database is Postgres on Neon. A tag that starts with `v` deploys through `.github/workflows/deploy.yml`.

Think of Cloud Run as a shop that opens only when a customer walks in. The first customer waits while the lights come on (the cold start). Later customers do not.

## The Image

The `Dockerfile` has two stages. The first installs Cograil and its dependencies into a virtualenv. The second copies only that virtualenv, the Workspace, and the Alembic migrations onto a clean `python:3.12-slim`. It runs as an unprivileged user (`cograil`) and listens on `$PORT` (Cloud Run sets it; the fallback is 8080). It trusts proxy headers from any address (`--forwarded-allow-ips='*'`), which is safe only behind Cloud Run. Do not publish the container port directly.

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
| `DATABASE_URL` | Secret Manager secret `cograil-database-url` |
| `ANTHROPIC_API_KEY` | Secret Manager secret `cograil-anthropic-api-key` |
| `COGRAIL_OIDC_CLIENT_SECRET` | Secret Manager secret `cograil-oidc-client-secret` |
| `COGRAIL_SESSION_SECRET` | Secret Manager secret `cograil-session-secret` |
| `COGRAIL_AUTH`, `COGRAIL_OIDC_*` (not secret) | Plain environment variables on the service, set once |

The workflow maps the four secrets on every deploy. It never reads their values, and GitHub never holds them. Plain variables you set on the service stay in place across deploys.

### Neon

Create a Neon project and a database, then store its connection string in `cograil-database-url` with the `asyncpg` driver and `ssl=require`:

```
postgresql+asyncpg://USER:PASSWORD@ep-example-123456.REGION.aws.neon.tech/DBNAME?ssl=require
```

asyncpg does not accept `sslmode=` or `channel_binding=`; leave them out. Neon suspends an idle database as well, so the first query after a quiet spell can add a short wait on top of the Cloud Run cold start.

Apply the migrations from a machine that can reach the database, with the image or a checkout. This also installs the `vector` extension:

```bash
docker run --rm -e DATABASE_URL="postgresql+asyncpg://..." cograil alembic upgrade head
```

## One-Time Setup

1. Create an Artifact Registry Docker repository named `cograil` in your region.
2. Create the four secrets above in Secret Manager.
3. Create a service account for deploys with `roles/run.admin`, `roles/artifactregistry.writer` and `roles/iam.serviceAccountUser` (on the runtime service account). Give the Cloud Run runtime service account `roles/secretmanager.secretAccessor` on the four secrets.
4. Create a Workload Identity pool and an OIDC provider for GitHub, restrict it to this repository with an attribute condition (`assertion.repository == 'cognitive-builder/cograil'`), and let the deploy service account be impersonated by that pool's identities. There are no JSON keys at any point: GitHub proves who it is with a short-lived token and Google swaps it for a short-lived credential.
5. Add four repository secrets: `GCP_PROJECT`, `GCP_REGION`, `GCP_WORKLOAD_IDENTITY_PROVIDER` (the provider's full resource name) and `GCP_SERVICE_ACCOUNT` (the deploy account's email).
6. Create the service once, before the first tag, with a placeholder image and the plain variables. Cograil refuses to start without `COGRAIL_AUTH`, so the first real revision needs them already in place:

   ```bash
   gcloud run deploy cograil --region REGION \
     --image us-docker.pkg.dev/cloudrun/container/hello \
     --set-env-vars COGRAIL_AUTH=oidc,COGRAIL_OIDC_METADATA_URL=...,COGRAIL_OIDC_CLIENT_ID=...,COGRAIL_OIDC_ALLOWED_DOMAINS=...,COGRAIL_OIDC_REDIRECT_URL=https://SERVICE_URL/auth/callback
   ```

   The service URL is only known after the first deploy, so deploy once, read the URL, then run `gcloud run services update cograil --update-env-vars COGRAIL_OIDC_REDIRECT_URL=...`.

The workflow deploys with `--allow-unauthenticated`. Browsers have to reach the sign-in page, and the app refuses every other request without a session. Never set `COGRAIL_AUTH=dev` on a deployed service.

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
