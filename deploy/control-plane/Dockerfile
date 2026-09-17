# The control plane: the API, the reaper, the publisher.
#
# **It ships the host package too, and that is not an oversight.** `patch_gate`
# is imported by the publisher and the publish worker on this side and by the
# runner and the snapshotter on the host side; the migration manifest called it
# host-only, which was wrong. Dropping `cloud_agent_host/` here produces an image that
# builds, starts, serves, and fails at the first publish — the furthest
# possible point from the mistake. See MIGRATION.md, "Manifest corrections".
#
# **No agent runtimes, no docker socket, no git credentials at build time.**
# This image never executes an agent and never checks a repository out. The one
# credential it holds at *runtime* is the git token used to publish, and that
# arrives as configuration, not as a layer.

FROM python:3.12-slim AS base

# Build-time only for asyncpg/cryptography wheels that may need compiling on
# architectures without one, plus git for the publish path's plumbing.
ARG DEBIAN_MIRROR=http://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR%/}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve them.
COPY pyproject.toml README.md LICENSE ./
COPY cloud_agent/__init__.py cloud_agent/__init__.py
COPY cloud_agent_host/__init__.py cloud_agent_host/__init__.py
ARG PIP_DEFAULT_TIMEOUT=120
RUN pip install --no-cache-dir .

# All three packages. See the header: `patch_gate` crosses the boundary.
COPY cloud_agent/ cloud_agent/
COPY cloud_agent_contracts/ cloud_agent_contracts/
COPY cloud_agent_host/ cloud_agent_host/

# Alembic owns the schema. Nothing in this service creates a table at startup,
# so these are not optional extras — an image without them can only run against
# a database somebody else migrated.
COPY alembic.ini ./
COPY migrations/ migrations/

RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 cloudagent \
    && chown -R cloudagent:cloudagent /app

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
