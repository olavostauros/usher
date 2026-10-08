"""Database roles the pipeline manages: ``grafana_ro``, ``api_ro`` and ``team``.

``grafana_ro`` and ``api_ro`` are the read-only roles (``READ_ONLY_ROLES``):
``SELECT`` on their views and nothing else. ``grafana_ro`` is the Grafana
datasource's (``docs/SPECIFICATION.md`` §6; migration ``0005``
creates it and grants the monitoring views), ``api_ro`` the events API's
(``docs/SPECIFICATION.md`` §7; migration ``0008``, the ``api_*`` views).
This module owns the part alembic cannot re-run, the passwords. Roles are
cluster-wide, so a scratch database's migration must never overwrite the
password the shared database's services use: the migration only sets it when
the role is new or still ``NOLOGIN``, and ``evcrawl migrate`` (the release
one-off, whose environment is the host ``.env``) applies it every time.
Both roles take any ``SELECT`` from outside (anonymous Grafana viewers
through ``/api/ds/query``, F-023 in
``docs/security/reports/2026-09-24-usher-stack.md``; the API's clients
through its query parameters), so each also carries bounds (its
``settings`` and a connection limit), set by the migrations ``0007`` and
``0008`` and by ``ensure_read_only_role``.

``team`` is the role every member and their agent logs in as from the ``dev``
container (F-014, ``docs/security/reports/2026-09-24-usher-stack.md``): row
privileges on the tables of ``public``, ``CREATE`` on the database for a
``<login>`` schema, ``CREATEDB`` for ``<login>_scratch``, and nothing of the
superuser's (no DDL or truncation in ``public``, no server programs or files,
no roles). It is not a migration: ``evcrawl migrate`` applies it after every
upgrade when it runs as a superuser (the release one-off), so the tables a
later migration adds are granted too; a member's migrate on scratch skips it.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import Connection, text

GRAFANA_ROLE = "grafana_ro"
PASSWORD_ENV = "GRAFANA_DB_PASSWORD"
API_ROLE = "api_ro"
API_PASSWORD_ENV = "EVCRAWL_API_DB_PASSWORD"
TEAM_ROLE = "team"
TEAM_PASSWORD_ENV = "TEAM_DB_PASSWORD"
# F-033: "0" on production, where no member connects; `team` stays NOLOGIN there.
TEAM_LOGIN_ENV = "TEAM_ROLE_LOGIN"
TEAM_TABLE_PRIVILEGES = "select, insert, update, delete"

# F-023: what one grafana_ro session may cost. statement_timeout, the idle
# timeout and work_mem are user-settable, so they stop runaway panels and
# casual misuse, not a determined client; temp_file_limit only a superuser can
# change, so it is the hard bound (with F-024's memory cap on `db`).
GRAFANA_ROLE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("statement_timeout", "30s"),
    ("idle_in_transaction_session_timeout", "60s"),
    ("work_mem", "16MB"),
    ("temp_file_limit", "256MB"),
)
GRAFANA_CONNECTION_LIMIT = 8

# Phase 4 spec §3: every api_ro session is read-only with a 3 s statement
# timeout. On the role, so they hold whatever the API process does; the rest
# are grafana_ro's bounds. The connection limit leaves the API's pool room.
API_ROLE_SETTINGS: tuple[tuple[str, str], ...] = (
    ("default_transaction_read_only", "on"),
    ("statement_timeout", "3s"),
    ("idle_in_transaction_session_timeout", "60s"),
    ("work_mem", "16MB"),
    ("temp_file_limit", "256MB"),
)
API_CONNECTION_LIMIT = 16


@dataclass(frozen=True)
class ReadOnlyRole:
    """A role that may only ``SELECT`` from its views, and how it is bounded."""

    name: str
    password_env: str
    settings: tuple[tuple[str, str], ...]
    connection_limit: int


GRAFANA = ReadOnlyRole(GRAFANA_ROLE, PASSWORD_ENV, GRAFANA_ROLE_SETTINGS, GRAFANA_CONNECTION_LIMIT)
API = ReadOnlyRole(API_ROLE, API_PASSWORD_ENV, API_ROLE_SETTINGS, API_CONNECTION_LIMIT)
READ_ONLY_ROLES: tuple[ReadOnlyRole, ...] = (GRAFANA, API)

log = logging.getLogger("evcrawl.db.roles")


def role_can_login(conn: Connection, role: str = GRAFANA_ROLE) -> bool | None:
    """``True``/``False`` for the role's ``LOGIN`` flag; ``None`` when it does not exist."""
    row = conn.execute(
        text("select rolcanlogin from pg_roles where rolname = :r"), {"r": role}
    ).scalar()
    return None if row is None else bool(row)


def _set_password(conn: Connection, role: str, password: str) -> None:
    quoted = conn.execute(text("select quote_literal(:p)"), {"p": password}).scalar_one()
    conn.execute(text(f"alter role {role} login password {quoted}"))


def is_superuser(conn: Connection) -> bool:
    return bool(
        conn.execute(text("select rolsuper from pg_roles where rolname = current_user")).scalar()
    )


def can_create_roles(conn: Connection) -> bool:
    return bool(
        conn.execute(
            text("select rolsuper or rolcreaterole from pg_roles where rolname = current_user")
        ).scalar()
    )


def bound_role(conn: Connection, role: ReadOnlyRole) -> str:
    """Apply ``role.settings`` and its connection limit (F-023 for ``grafana_ro``).

    Idempotent. Only a superuser may set ``temp_file_limit`` on a role, so
    connected as anything else (a member's migrate on their scratch database,
    as ``team``) it returns ``"skipped"``: the role is cluster-wide, and the
    release migrate of the shared database has bounded it already. The new
    settings apply to sessions opened after this.
    """
    if not is_superuser(conn):
        return "skipped"
    for name, value in role.settings:
        conn.execute(text(f"alter role {role.name} set {name} = '{value}'"))
    conn.execute(text(f"alter role {role.name} connection limit {role.connection_limit}"))
    return "bounded"


def bound_grafana_role(conn: Connection) -> str:
    """``bound_role`` for ``grafana_ro`` (migration ``0007``)."""
    return bound_role(conn, GRAFANA)


def ensure_read_only_role(
    conn: Connection, role: ReadOnlyRole, password: str | None, *, rotate: bool
) -> str:
    """Create the role if absent, bound it and set its password; return what was done.

    ``rotate=False`` (the migrations) applies ``password`` only to a role that
    cannot log in yet; ``rotate=True`` (``evcrawl migrate``) applies it to an
    existing role too. Without a password a new role is ``NOLOGIN``, which is
    what scratch databases and CI get: the grants and the tests still exercise
    it, and the first migrate with the password turns login on. A role that
    does not exist yet and cannot be created here (a member's migrate as
    ``team``, before the shared database's release migrate has created it) is
    ``"missing"``; the caller leaves its grants out.
    """
    can_login = role_can_login(conn, role.name)
    if can_login is None:
        if not can_create_roles(conn):
            log.warning("%s does not exist and this login cannot create it", role.name)
            return "missing"
        conn.execute(
            text(f"create role {role.name} nologin nosuperuser nocreatedb nocreaterole noinherit")
        )
        can_login = False
        done = "created"
    else:
        done = "exists"
    if bound_role(conn, role) == "bounded":
        done += ", bounded"
    if not password:
        if not can_login:
            log.warning(
                "%s unset: %s has no login (fine on scratch and in CI)",
                role.password_env,
                role.name,
            )
        return done
    if can_login and not rotate:
        return done
    _set_password(conn, role.name, password)
    return f"{done}, login set"


def ensure_grafana_role(conn: Connection, password: str | None, *, rotate: bool) -> str:
    """``ensure_read_only_role`` for ``grafana_ro`` (migration ``0005``, ``evcrawl migrate``)."""
    return ensure_read_only_role(conn, GRAFANA, password, rotate=rotate)


def ensure_team_role(conn: Connection, password: str | None, *, login: bool = True) -> str:
    """Create ``team`` if absent, set its password, grant it this database; return what was done.

    Runs only as a superuser; connected as anything else (a member's migrate
    on their scratch database runs as ``team`` itself) it returns
    ``"skipped"``. Without a password a new role is ``NOLOGIN``, like
    ``grafana_ro``. With ``login=False`` (production, F-033) the role is
    kept ``NOLOGIN`` and the password is ignored; the grants stay. The grants
    are idempotent and applied every time: the ``all tables`` grant covers
    what exists, the default privileges what the migrating role creates later.
    """
    if not is_superuser(conn):
        return "skipped"
    if role_can_login(conn, TEAM_ROLE) is None:
        conn.execute(
            text(
                f"create role {TEAM_ROLE} nologin nosuperuser nocreaterole createdb"
                " noreplication nobypassrls"
            )
        )
        done = "created"
    else:
        done = "exists"
    if not login:
        conn.execute(text(f"alter role {TEAM_ROLE} nologin"))
        done += ", nologin"
    elif password:
        _set_password(conn, TEAM_ROLE, password)
        done += ", login set"
    db = conn.execute(text("select quote_ident(current_database())")).scalar_one()
    for grant in (
        f"grant connect, temporary, create on database {db} to {TEAM_ROLE}",
        f"grant usage on schema public to {TEAM_ROLE}",
        f"grant {TEAM_TABLE_PRIVILEGES} on all tables in schema public to {TEAM_ROLE}",
        f"grant usage, select on all sequences in schema public to {TEAM_ROLE}",
        "alter default privileges in schema public"
        f" grant {TEAM_TABLE_PRIVILEGES} on tables to {TEAM_ROLE}",
        "alter default privileges in schema public"
        f" grant usage, select on sequences to {TEAM_ROLE}",
    ):
        conn.execute(text(grant))
    for role in READ_ONLY_ROLES:
        if role_can_login(conn, role.name) is not None:
            # The DB-backed tests `set role grafana_ro` / `api_ro` on a member's
            # scratch database. Both read less than team does: this grants nothing.
            conn.execute(text(f"grant {role.name} to {TEAM_ROLE}"))
    return done
