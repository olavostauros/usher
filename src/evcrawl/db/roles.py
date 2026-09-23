"""Database roles the pipeline manages: today only ``grafana_ro``.

``grafana_ro`` is the read-only role the Grafana datasource logs in with
(``docs/specs/phase-3-monitoring.md`` §3): ``SELECT`` on the monitoring views
and nothing else. Migration ``0005`` creates it and grants the views; this
module owns the part alembic cannot re-run, the password. Roles are
cluster-wide, so a scratch database's migration must never overwrite the
password the shared database's Grafana uses: the migration only sets it when
the role is new or still ``NOLOGIN``, and ``evcrawl migrate`` (the release
one-off, whose environment is the host ``.env``) applies it every time.
"""

import logging

from sqlalchemy import Connection, text

GRAFANA_ROLE = "grafana_ro"
PASSWORD_ENV = "GRAFANA_DB_PASSWORD"

log = logging.getLogger("evcrawl.db.roles")


def role_can_login(conn: Connection) -> bool | None:
    """``True``/``False`` for the role's ``LOGIN`` flag; ``None`` when it does not exist."""
    row = conn.execute(
        text("select rolcanlogin from pg_roles where rolname = :r"), {"r": GRAFANA_ROLE}
    ).scalar()
    return None if row is None else bool(row)


def ensure_grafana_role(conn: Connection, password: str | None, *, rotate: bool) -> str:
    """Create ``grafana_ro`` if absent and set its password; return what was done.

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
    if not password:
        if not can_login:
            log.warning(
                "%s unset: %s has no login (fine on scratch and in CI)", PASSWORD_ENV, GRAFANA_ROLE
            )
        return done
    if can_login and not rotate:
        return done
    quoted = conn.execute(text("select quote_literal(:p)"), {"p": password}).scalar_one()
    conn.execute(text(f"alter role {GRAFANA_ROLE} login password {quoted}"))
    return f"{done}, login set"
