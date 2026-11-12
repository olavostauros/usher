# syntax=docker/dockerfile:1.7
# Builds the evcrawl image: the release artifact of this repository.
# Multi-stage: uv resolves the locked dependency set in the builder; the
# runtime stage carries only the virtualenv, the package, and the migrations.
# Base and uv are pinned by tag plus multi-arch index digest (F-002); bump the
# three FROM lines together, and the uv tag in both COPY lines with .mise.toml
# (AGENTS.md §2). The python:3.12-slim digest is the 2026-10-06 build, which
# carries OpenSSL 3.5.7-1~deb13u3 (DSA-6531-1, F-016/F-032).

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.33@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first so the layer caches independently of source edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md ./README.md
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---------------------------------------------------------------------------
# dev: the hosted team workspace. One long-running container on the host;
# members SSH in (key auth only), each with their own user, home, and clone.
# Membership is data under team/ and is applied by team/entrypoint.sh on boot.
# ---------------------------------------------------------------------------
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS dev

COPY --from=ghcr.io/astral-sh/uv:0.11.33@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /uvx /usr/local/bin/

# The find drops every setuid/setgid bit (su, passwd, mount, ssh-keysign, ...):
# key-only sshd and the root entrypoint need none, and each is an escalation
# path if no-new-privileges is ever dropped (F-007).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server git tmux postgresql-client redis-tools ripgrep jq unzip \
        ca-certificates curl less vim procps openssl \
    && rm -rf /var/lib/apt/lists/* \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && mkdir -p /run/sshd /etc/ssh/host_keys /opt/uv-cache \
    && groupadd --system team

# GitHub CLI, pinned: agents open their own PRs from inside the workspace
# (AGENTS.md §4, "Agents in the workspace"). Members authenticate per user
# with `gh auth login`; the token lives in their own $HOME.
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
RUN curl -fsSL -o /tmp/gh.tgz \
        "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    && echo "${GH_SHA256}  /tmp/gh.tgz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tgz -C /usr/local --strip-components=1 \
        "gh_${GH_VERSION}_linux_amd64/bin/gh" \
    && rm /tmp/gh.tgz \
    && gh --version

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/opt/uv-cache \
    PYTHONUNBUFFERED=1

# Pre-warm a read-only uv cache. Members never write to it: each has their own
# cache at ~/.cache/uv, seeded from this one on first login by
# /etc/profile.d/evcrawl.sh (team/entrypoint.sh). A shared writable cache let
# one member plant code in every other member's .venv (F-008,
# docs/security/reports/2026-09-24-usher-stack.md). UV_CACHE_DIR above is for
# root only (the image build and CI's gates run); sshd sessions do not see it.
COPY pyproject.toml uv.lock /tmp/warm/
RUN --mount=type=cache,target=/root/.cache/uv \
    cd /tmp/warm && uv sync --frozen --no-install-project \
    && rm -rf /tmp/warm && chmod -R a+rX,go-w /opt/uv-cache

COPY team/sshd.conf /etc/ssh/sshd_config.d/team.conf
COPY team/limits.conf /etc/security/limits.d/team.conf
COPY team/entrypoint.sh /usr/local/bin/team-entrypoint
COPY team/ /opt/team/
RUN chmod 0755 /usr/local/bin/team-entrypoint \
    && sed -i 's#^\(HostKey /etc/ssh/ssh_host_\)#\#\1#' /etc/ssh/sshd_config

EXPOSE 22
ENTRYPOINT ["team-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]


FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS runtime

ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="evcrawl" \
      org.opencontainers.image.description="Evento Livre event crawler" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    EVCRAWL_GIT_SHA="${GIT_SHA}"

RUN groupadd --system --gid 1001 evcrawl \
    && useradd --system --uid 1001 --gid evcrawl --home-dir /app --no-create-home evcrawl

WORKDIR /app
COPY --from=builder --chown=evcrawl:evcrawl /app/.venv ./.venv
COPY --from=builder --chown=evcrawl:evcrawl /app/src ./src
COPY --from=builder --chown=evcrawl:evcrawl /app/alembic.ini ./alembic.ini
COPY --from=builder --chown=evcrawl:evcrawl /app/alembic ./alembic

USER evcrawl

ENTRYPOINT ["evcrawl"]
CMD ["scheduler"]
