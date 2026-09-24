"""security: bound what one grafana_ro session may cost (F-023)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-24

``docs/security/reports/2026-09-24-usher-stack.md`` F-023: Grafana OSS gives
every Viewer ``datasources:query``, so an anonymous viewer can send any
``SELECT`` to the ``evcrawl`` datasource through ``/api/ds/query``. What it
can read is bounded by ``grafana_ro``'s grants (the views only, ``0005`` and
``0006``); what it can cost was not: no statement timeout, no connection
limit. This sets ``evcrawl.db.roles.GRAFANA_ROLE_SETTINGS`` and the
connection limit on the role, the same statements ``ensure_grafana_role``
applies to a role it creates.

Roles are cluster-wide and only a superuser may set ``temp_file_limit`` on
one, so a member's migrate of their scratch database (as ``team``) skips the
role and leaves the bounds the shared database's release migrate set. The
downgrade leaves them too: resetting them from a scratch database would
unbound the role the shared Grafana logs in with.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from evcrawl.db.roles import GRAFANA_ROLE, bound_grafana_role

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("select 1 from pg_roles where rolname = :r"), {"r": GRAFANA_ROLE}
    ).scalar()
    if exists is None:  # 0005 creates it; nothing to bound without it
        return
    log.info("%s: %s", GRAFANA_ROLE, bound_grafana_role(conn))


def downgrade() -> None:
    pass
