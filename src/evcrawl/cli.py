"""The ``evcrawl`` command-line entry point (the image's ENTRYPOINT)."""

import asyncio
from pathlib import Path

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

    Exit code 1 when a source discovered events but could not fetch any of them;
    2 for an unknown source or city.
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


if __name__ == "__main__":
    app()
