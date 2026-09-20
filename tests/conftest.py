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
from sqlalchemy import Engine, create_engine

from evcrawl.cli import ALEMBIC_INI

SCRATCH_URL = os.environ.get("EVCRAWL_TEST_DATABASE_URL")


def alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="session")
def scratch_engine() -> Iterator[Engine]:
    """Engine on the scratch database, migrated to head; downgraded to base afterwards."""
    if not SCRATCH_URL:
        pytest.skip("EVCRAWL_TEST_DATABASE_URL not set (use a <login>_scratch database)")
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
