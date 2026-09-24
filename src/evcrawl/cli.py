"""The ``evcrawl`` command-line entry point (the image's ENTRYPOINT)."""

import asyncio
from pathlib import Path
from typing import Any

import typer

from evcrawl import __version__
from evcrawl.config import get_settings
from evcrawl.logging import configure_logging, get_logger

app = typer.Typer(
    name="evcrawl",
    help="Evento Livre event crawler: crawl sources, geocode, dedupe, and maintain the database.",
    no_args_is_help=True,
)

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@app.callback()
def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)


@app.command()
def version() -> None:
    """Print the package version and the git revision baked into the image."""
    typer.echo(f"evcrawl {__version__} ({get_settings().evcrawl_git_sha})")


@app.command()
def migrate() -> None:
    """Apply Alembic migrations up to head. Run as a one-off before the scheduler."""
    from alembic import command
    from alembic.config import Config

    log = get_logger("evcrawl.migrate")
    log.info("migrate.start", ini=str(ALEMBIC_INI))
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    log.info("migrate.done")
    _sync_roles(log)


def _sync_roles(log: Any) -> None:
    """Apply the roles' passwords and ``team``'s grants after every migrate (0005, 0008 run once).

    ``GRAFANA_DB_PASSWORD`` goes to ``grafana_ro`` once 0005 has created it,
    ``EVCRAWL_API_DB_PASSWORD`` to ``api_ro`` once 0008 has; ``team`` (F-014)
    is created and granted this database when the migrate runs as a
    superuser, with ``TEAM_DB_PASSWORD``.
    """
    import os

    from evcrawl.db.roles import (
        READ_ONLY_ROLES,
        TEAM_PASSWORD_ENV,
        TEAM_ROLE,
        ensure_read_only_role,
        ensure_team_role,
        role_can_login,
    )
    from evcrawl.db.session import get_engine

    with get_engine().begin() as conn:
        for role in READ_ONLY_ROLES:
            password = os.environ.get(role.password_env)
            if (
                role_can_login(conn, role.name) is None
            ):  # its migration not applied: nothing to sync
                pass
            elif not password:
                log.warning("migrate.role", role=role.name, env=role.password_env, status="unset")
            else:
                log.info(
                    "migrate.role",
                    role=role.name,
                    status=ensure_read_only_role(conn, role, password, rotate=True),
                )
        # After the read-only roles, so a first migrate already grants them to team.
        status = ensure_team_role(conn, os.environ.get(TEAM_PASSWORD_ENV) or None)
        log.info("migrate.role", role=TEAM_ROLE, status=status)


@app.command()
def scheduler() -> None:
    """Run the crawl scheduler in the foreground (the image's default command)."""
    from evcrawl.scheduler import run_forever

    asyncio.run(run_forever(get_settings()))


@app.command()
def crawl(
    source: str = typer.Argument(help="Source adapter name, or 'all'."),
    city: str | None = typer.Option(
        None, help="Restrict discovery to one registry city (slug or name, e.g. sao-paulo)."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch and parse without writing."),
    limit: int | None = typer.Option(None, help="Stop after this many events."),
) -> None:
    """Crawl one source (or all registered sources): discover, fetch, normalize, geocode, upsert.

    Exit code 1 when a source discovered events but fetched none of them, unless
    every one was skipped as unchanged without an error; 2 for an unknown source
    or city.
    """
    from evcrawl.cities import City, resolve, slugs
    from evcrawl.db.models import CrawlKind
    from evcrawl.pipeline.crawl import run_crawl
    from evcrawl.sources import REGISTRY

    log = get_logger("evcrawl.crawl")
    names = list(REGISTRY) if source == "all" else [source]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        log.error("crawl.unknown_source", sources=unknown, known=sorted(REGISTRY))
        raise typer.Exit(code=2)
    target: City | None = None
    if city is not None:
        try:
            target = resolve(city)
        except ValueError:
            log.error("crawl.unknown_city", city=city, known=slugs())
            raise typer.Exit(code=2) from None
    failed = False
    for name in names:
        # A hand-run crawl is never what freshness is measured on (spec 3's SLA
        # reads ``crawl`` rows), so the CLI — including the release smoke — is ``manual``.
        summary = asyncio.run(
            run_crawl(name, city=target, kind=CrawlKind.MANUAL, dry_run=dry_run, limit=limit)
        )
        failed = failed or summary.failed
    if failed:
        raise typer.Exit(code=1)


def openapi_json() -> str:
    """The API's OpenAPI document as committed in ``docs/api/openapi.json``."""
    import json

    from evcrawl.api.app import create_app

    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


@app.command()
def api(
    host: str = typer.Option("127.0.0.1", help="Address to listen on (0.0.0.0 in the container)."),
    port: int = typer.Option(8000, help="Port to listen on."),
    openapi: bool = typer.Option(
        False, "--openapi", help="Print the OpenAPI document and exit (docs/api/openapi.json)."
    ),
) -> None:
    """Serve the read-only events API (Phase 4) as api_ro on EVCRAWL_API_DATABASE_URL.

    Exit code 2 when EVCRAWL_API_DATABASE_URL is unset: the API never falls
    back to DATABASE_URL.
    """
    if openapi:
        typer.echo(openapi_json(), nl=False)
        return
    import uvicorn

    from evcrawl.api.app import create_app

    log = get_logger("evcrawl.api")
    if not get_settings().evcrawl_api_database_url:
        log.error("api.no_database", env="EVCRAWL_API_DATABASE_URL")
        raise typer.Exit(code=2)
    # No access log: uvicorn's carries client addresses and query strings (the
    # user's position). The app logs one line per request without either.
    uvicorn.run(create_app(), host=host, port=port, access_log=False, log_config=None)


if __name__ == "__main__":
    app()
