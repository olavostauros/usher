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
    city: str | None = typer.Option(None, help="Restrict discovery to one city."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch and parse without writing."),
    limit: int | None = typer.Option(None, help="Stop after this many events."),
) -> None:
    """Crawl one source (or all registered sources)."""
    from evcrawl.sources import REGISTRY

    log = get_logger("evcrawl.crawl")
    names = list(REGISTRY) if source == "all" else [source]
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        log.error("crawl.unknown_source", sources=unknown, known=sorted(REGISTRY))
        raise typer.Exit(code=2)
    for name in names:
        log.info("crawl.start", source=name, city=city, dry_run=dry_run, limit=limit)
        log.warning("crawl.not_implemented", source=name)


if __name__ == "__main__":
    app()
