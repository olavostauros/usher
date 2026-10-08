#!/usr/bin/env bash
# The CI gates (.github/workflows/ci.yml) inside the dev container, the
# DB-backed tests included: ruff, ruff format, mypy, the `team` role checks and
# pytest with EVCRAWL_REQUIRE_TEST_DATABASE=1 on <login>_scratch (AGENTS.md §5,
# §7). What CI cannot show before a PR is open, and the host cannot run at all
# (no route to the compose `db`).
#
# Inside the container, from your clone:
#   ops/dev/gates.sh                 # the working tree as it is, uncommitted edits included
#   ops/dev/gates.sh <branch>        # origin/<branch> in a throwaway worktree; the clone is untouched
# From the host owner's session (the script travels on stdin, so it need not be merged yet):
#   ssh usher bash -s -- <branch> < ops/dev/gates.sh
#
# Writes only to <login>_scratch (created from template_postgis if missing) and,
# for a branch, to a worktree under ~/.cache/usher-gates that is removed on exit.
# Exit 0 when every gate passes; the first failing gate stops the run.
set -euo pipefail

if [ "$(hostname)" != evcrawl-dev ]; then
    echo "gates.sh runs inside the dev container (hostname evcrawl-dev), not on $(hostname)" >&2
    exit 2
fi
: "${DATABASE_URL:?DATABASE_URL is preset in the dev container}"

repo=${USHER_CLONE:-$HOME/usher}
branch=${1:-}

if [ -n "$branch" ]; then
    git -C "$repo" fetch -q origin "$branch"
    mkdir -p "$HOME/.cache/usher-gates"
    wt=$(mktemp -d "$HOME/.cache/usher-gates/${branch//\//-}.XXXXXX")
    trap 'git -C "$repo" worktree remove --force "$wt"' EXIT
    git -C "$repo" worktree add -q --detach "$wt" FETCH_HEAD
    cd "$wt"
    echo "gates on origin/$branch at $(git log -1 --format='%h %s')"
else
    cd "$repo"
    echo "gates on the working tree of $repo ($(git branch --show-current) at $(git log -1 --format=%h))"
fi

scratch="${USER}_scratch"
if ! psql -X -lqtA | cut -d'|' -f1 | grep -qx "$scratch"; then
    createdb -T template_postgis "$scratch"
fi
export EVCRAWL_TEST_DATABASE_URL="${DATABASE_URL%/*}/$scratch"
export EVCRAWL_REQUIRE_TEST_DATABASE=1

step() { printf '\n== %s\n' "$*"; }
step "uv sync";            uv sync --frozen -q
step "ruff check";         uv run ruff check .
step "ruff format";        uv run ruff format --check .
step "mypy";               uv run mypy
step "team role checks";   bash ops/db/team-role-checks.sh
step "pytest";             uv run pytest -q -rs
printf '\nall gates passed\n'
