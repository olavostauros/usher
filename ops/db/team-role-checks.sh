#!/usr/bin/env bash
# What the `team` role may and may not do (F-014, docs/security/reports/
# 2026-09-24-usher-stack.md; src/evcrawl/db/roles.py). Runs with the PG*
# variables: as a member inside the dev container it checks the shared
# database, in CI the stand-in one. Safe on the shared database: every
# statement runs in a transaction that is rolled back, the row checks touch no
# row (`where false`) and the program it tries to run is `true`.
# Exit 0 when every check holds; prints one line per check.
set -euo pipefail

fail=0
ok() { printf 'ok      %s\n' "$1"; }
bad() { printf 'FAIL    %s\n' "$1"; fail=1; }

# allowed <label> <sql>: the statement must succeed.
allowed() {
  if out=$(psql -X -q -v ON_ERROR_STOP=1 -c "begin" -c "$2" -c "rollback" 2>&1); then
    ok "$1"
  else
    bad "$1: $out"
  fi
}

# denied <label> <sql>: the statement must fail with a permission error.
denied() {
  if out=$(psql -X -q -v ON_ERROR_STOP=1 -c "begin" -c "$2" -c "rollback" 2>&1); then
    bad "$1: allowed"
  elif grep -qiE 'permission denied|must be (superuser|owner)' <<<"$out"; then
    ok "$1 (denied)"
  else
    bad "$1: failed, but not on a permission: $out"
  fi
}

role=$(psql -X -Atc "select current_user")
[ "$(psql -X -Atc "select rolsuper::text from pg_roles where rolname = current_user")" = false ] \
  && ok "$role is not a superuser" || bad "$role is a superuser"
[ "$(psql -X -Atc "select rolcreaterole::text from pg_roles where rolname = current_user")" = false ] \
  && ok "$role cannot create roles" || bad "$role can create roles"

allowed "read public" "select count(*) from events"
allowed "insert into public" "insert into crawl_runs select * from crawl_runs where false"
allowed "update public" "update crawl_runs set source = source where false"
allowed "delete from public" "delete from crawl_runs where false"
allowed "own schema" "create schema ${USER:-$role}_role_check; create table ${USER:-$role}_role_check.t (id int)"

denied "create a table in public" "create table public.team_role_check (id int)"
denied "truncate a table in public" "truncate crawl_runs"
denied "drop a table in public" "drop table crawl_runs"
denied "run a server program" "copy (select 1) to program 'true'"
denied "read a server file" "select pg_read_file('/etc/hostname')"
denied "read pg_authid" "select rolpassword from pg_authid limit 1"
denied "create a role" "create role team_role_check"

exit "$fail"
