"""Shared fixtures.

Database tests run only when ``EVCRAWL_TEST_DATABASE_URL`` points at a throwaway
database (``createdb <login>_scratch``, AGENTS.md §7); they are skipped otherwise
so the offline gates never touch the shared ``db`` service.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from evcrawl.cli import ALEMBIC_INI
from evcrawl.db.models import Base

SCRATCH_URL = os.environ.get("EVCRAWL_TEST_DATABASE_URL")


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def _scratch_engine() -> Iterator[Engine]:
    """Engine on the scratch database, migrated to head; downgraded to base afterwards."""
    if not SCRATCH_URL:
        pytest.skip("EVCRAWL_TEST_DATABASE_URL not set (use a <login>_scratch database)")
    if not SCRATCH_URL.rsplit("/", 1)[-1].split("?")[0].endswith("_scratch"):
        # Tests migrate up and down and clear tables: never the shared db (AGENTS.md §7).
        pytest.fail("EVCRAWL_TEST_DATABASE_URL must point at a database named <login>_scratch")
    os.environ["DATABASE_URL"] = SCRATCH_URL  # alembic/env.py reads settings
    from evcrawl.config import get_settings

    get_settings.cache_clear()
    cfg = alembic_config(SCRATCH_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(SCRATCH_URL)
    try:
        yield engine
    finally:
        engine.dispose()
        command.downgrade(cfg, "base")


@pytest.fixture
def scratch_engine(_scratch_engine: Engine) -> Engine:
    """The session engine with every model table emptied, so tests never see each other's rows."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with _scratch_engine.begin() as conn:
        conn.execute(text(f"truncate {tables} cascade"))  # scratch db only (see _scratch_engine)
    return _scratch_engine
