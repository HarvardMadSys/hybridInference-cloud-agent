# syntax=docker/dockerfile:1.7
# The control plane: the API, the reaper, the publisher.
#
# **It ships the host package too, and that is not an oversight.**
# `cloud_agent/api/routes.py` still imports the runtime registry —
# `registered_runtimes`, `runtime_reasoning_efforts` and
# `runtimes_supporting_mcp` — from `cloud_agent_host.runtimes`, and
# `create_app()` imports that router, so dropping `cloud_agent_host/` here
# produces an image whose control plane cannot start. The publisher's patch
# gate and archive builders are no longer part of that reach: they live in
# `cloud_agent_shared/git/`, which this image and the runner image both ship. See
# MIGRATION.md, "Manifest corrections".
#
# **No agent runtimes, no docker socket, no git credentials at build time.**
# This image never executes an agent and never checks a repository out.
# Publishing and optional compute lifecycle credentials arrive at runtime,
# through configuration or the encrypted credential store, never as a layer.

# Keep build inputs out of the runtime filesystem except for the explicit,
# credential-free archive consumed by automatic Server setup. Its revision is
# the same argument used by this image's OCI label below.
FROM python:3.12-slim AS host-release
ARG SOURCE_COMMIT=unknown
COPY deploy/release/build_host_bundle.py /builder/build_host_bundle.py
RUN --mount=type=bind,source=.,target=/source,ro \
    python /builder/build_host_bundle.py --root /source --source-commit "${SOURCE_COMMIT}" \
    --output /compute-host-release --allow-unknown

FROM python:3.12-slim AS base

# Build-time only for asyncpg/cryptography wheels that may need compiling on
# architectures without one, plus git for the publish path's plumbing.
ARG DEBIAN_MIRROR=http://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR%/}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install the same locked production dependencies CI and the runner use.
# Source edits retain this dependency layer; neither sync may update uv.lock.
ARG PIP_DEFAULT_TIMEOUT=120
ARG UV_HTTP_TIMEOUT=120
ARG UV_VERSION=0.9.7
RUN pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# All three packages. See the header for why this image needs the host one.
COPY apps/backend/cloud_agent/ apps/backend/cloud_agent/
COPY apps/backend/cloud_agent_shared/ apps/backend/cloud_agent_shared/
COPY apps/backend/cloud_agent_host/ apps/backend/cloud_agent_host/

# Alembic owns the schema. Nothing in this service creates a table at startup,
# so these are not optional extras — an image without them can only run against
# a database somebody else migrated.
COPY alembic.ini ./
COPY migrations/ migrations/
COPY --from=host-release /compute-host-release/ /app/compute-host-release/

RUN uv sync --frozen --no-dev --no-editable \
    && useradd --create-home --uid 10001 cloudagent \
    && chown -R cloudagent:cloudagent /app \
    && install -d -m 0750 -o cloudagent -g cloudagent /var/lib/cloud-agent/pgadmin-bootstrap

# Never root. This process holds a git credential and a database; a container
# escape that starts as root is a materially worse day than one that does not.
# What this image is, in the two places a deployment looks: OCI's own labels,
# and one of ours naming the artifact's role. A resolver reads both out of the
# platform image it is about to run and compares them with the release
# manifest — the revision against the manifest's source commit, the role
# against the artifact it stands in for — so a build matrix that swaps two
# digests is caught where the release is consumed rather than where it is
# deployed. `SOURCE_COMMIT` is supplied by the publication build; an image
# built by hand says `unknown`, and a resolver refuses it.
ARG SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.source="https://github.com/HarvardMadSys/hybridInference-cloud-agent" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      io.hybridinference.artifact.role="control"

USER cloudagent

EXPOSE 8000

# `--proxy-headers` is deliberately absent. Whether to believe a forwarded
# address is `AGENT_TRUST_PROXY_HEADERS`, decided by the deployment that knows
# whether a proxy it controls actually overwrites the header — baking it in
# here would let any caller write the admin audit trail's client IP.
CMD ["uvicorn", "cloud_agent.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
