"""Shared fixtures.

Database tests run only when ``EVCRAWL_TEST_DATABASE_URL`` points at a throwaway
database (``createdb -T template_postgis <login>_scratch``, AGENTS.md §7); they are
skipped otherwise so the offline gates never touch the shared ``db`` service. Where the database is
meant to be there — CI, which starts one for the job — set
``EVCRAWL_REQUIRE_TEST_DATABASE=1`` and the skip becomes a failure, so losing the
service is loud instead of a third of the suite quietly not running.
"""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text

from evcrawl import cities
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
        missing = "EVCRAWL_TEST_DATABASE_URL not set (use a <login>_scratch database)"
        if os.environ.get("EVCRAWL_REQUIRE_TEST_DATABASE"):
            pytest.fail(f"{missing}; EVCRAWL_REQUIRE_TEST_DATABASE is set, so this is an error")
        pytest.skip(missing)
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


@pytest.fixture
def greater_vitoria_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the registry to the six ES cities for the tests whose fixtures count them.

    Every capital is enabled since 2026-09-23; a national fixture (Zig's feed,
    TicketBolt's list) carries other capitals' events too, so ``discover(None)``
    only equals "all of Greater Vitória" under this fixture.
    """
    monkeypatch.setattr(cities, "CITIES", cities.GREATER_VITORIA)
