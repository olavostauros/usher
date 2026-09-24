"""Database roles the pipeline manages: ``grafana_ro`` and ``team``.

``grafana_ro`` is the read-only role the Grafana datasource logs in with
(``docs/specs/phase-3-monitoring.md`` §3): ``SELECT`` on the monitoring views
and nothing else. Migration ``0005`` creates it and grants the views; this
module owns the part alembic cannot re-run, the password. Roles are
cluster-wide, so a scratch database's migration must never overwrite the
password the shared database's Grafana uses: the migration only sets it when
the role is new or still ``NOLOGIN``, and ``evcrawl migrate`` (the release
one-off, whose environment is the host ``.env``) applies it every time.
Anonymous Grafana viewers can send any ``SELECT`` to the datasource through
``/api/ds/query`` (F-023, ``docs/security/reports/2026-09-24-usher-stack.md``),
so the role also carries bounds (``GRAFANA_ROLE_SETTINGS``, a connection
limit), set by migration ``0007`` and by ``ensure_grafana_role``.

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

from sqlalchemy import Connection, text

GRAFANA_ROLE = "grafana_ro"
PASSWORD_ENV = "GRAFANA_DB_PASSWORD"
TEAM_ROLE = "team"
TEAM_PASSWORD_ENV = "TEAM_DB_PASSWORD"
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


def bound_grafana_role(conn: Connection) -> str:
    """Apply ``GRAFANA_ROLE_SETTINGS`` and the connection limit to ``grafana_ro`` (F-023).

    Idempotent. Only a superuser may set ``temp_file_limit`` on a role, so
    connected as anything else (a member's migrate on their scratch database,
    as ``team``) it returns ``"skipped"``: the role is cluster-wide, and the
    release migrate of the shared database has bounded it already. The new
    settings apply to sessions opened after this.
    """
    if not is_superuser(conn):
        return "skipped"
    for name, value in GRAFANA_ROLE_SETTINGS:
        conn.execute(text(f"alter role {GRAFANA_ROLE} set {name} = '{value}'"))
    conn.execute(text(f"alter role {GRAFANA_ROLE} connection limit {GRAFANA_CONNECTION_LIMIT}"))
    return "bounded"


def ensure_grafana_role(conn: Connection, password: str | None, *, rotate: bool) -> str:
    """Create ``grafana_ro`` if absent, bound it and set its password; return what was done.

    ``rotate=False`` (the migration) applies ``password`` only to a role that
    cannot log in yet; ``rotate=True`` (``evcrawl migrate``) applies it to an
    existing role too. Without a password a new role is ``NOLOGIN``, which is
    what scratch databases and CI get: the grants and the tests still exercise
    it, and the first migrate with the password turns login on.
    """
    can_login = role_can_login(conn)
    if can_login is None:
        conn.execute(
            text(
                f"create role {GRAFANA_ROLE} nologin nosuperuser nocreatedb nocreaterole noinherit"
            )
        )
        can_login = False
        done = "created"
    else:
        done = "exists"
    if bound_grafana_role(conn) == "bounded":
        done += ", bounded"
    if not password:
        if not can_login:
            log.warning(
                "%s unset: %s has no login (fine on scratch and in CI)", PASSWORD_ENV, GRAFANA_ROLE
            )
        return done
    if can_login and not rotate:
        return done
    _set_password(conn, GRAFANA_ROLE, password)
    return f"{done}, login set"


def ensure_team_role(conn: Connection, password: str | None) -> str:
    """Create ``team`` if absent, set its password, grant it this database; return what was done.

    Runs only as a superuser; connected as anything else (a member's migrate
    on their scratch database runs as ``team`` itself) it returns
    ``"skipped"``. Without a password a new role is ``NOLOGIN``, like
    ``grafana_ro``. The grants are idempotent and applied every time: the
    ``all tables`` grant covers what exists, the default privileges what the
    migrating role creates later.
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
    if password:
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
    if role_can_login(conn, GRAFANA_ROLE) is not None:
        # The DB-backed tests `set role grafana_ro` on a member's scratch
        # database. grafana_ro reads less than team does: this grants nothing.
        conn.execute(text(f"grant {GRAFANA_ROLE} to {TEAM_ROLE}"))
    return done
