# Multi-stage image for Cloud Run. The builder installs Cograil into a virtualenv;
# the runtime stage copies only that virtualenv, the Workspace and the migrations.
#
#   docker build -t cograil .
#   docker build --build-arg WORKSPACE=workspaces/my-pack --build-arg EXTRAS=oidc,slack -t cograil .

FROM python:3.12-slim AS builder
ARG EXTRAS=oidc
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# --without-pip keeps the copied virtualenv about 13 MB smaller; pip in this stage does the install.
RUN python -m venv --without-pip /opt/venv \
 && pip --python /opt/venv/bin/python install --no-cache-dir ".[${EXTRAS}]"

FROM python:3.12-slim AS runtime
ARG WORKSPACE=workspaces/example-smb
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COGRAIL_WORKSPACE=/workspace
RUN useradd --system --uid 10001 --no-create-home cograil
COPY --from=builder /opt/venv /opt/venv
COPY ${WORKSPACE} /workspace
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations
WORKDIR /app
USER cograil
# DATABASE_URL, COGRAIL_AUTH and the secrets come from the Cloud Run environment, never the image.
# Cloud Run sets PORT; 8080 is the fallback for local runs.
CMD ["sh", "-c", "exec uvicorn --factory cograil.api.wiring:app_from_env --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
