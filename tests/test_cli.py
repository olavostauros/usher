from typer.testing import CliRunner

from evcrawl import __version__
from evcrawl.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("crawl", "migrate", "scheduler", "version"):
        assert command in result.output


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_crawl_rejects_unknown_source() -> None:
    result = runner.invoke(app, ["crawl", "nope", "--dry-run"])
    assert result.exit_code == 2


def test_crawl_rejects_unknown_city() -> None:
    result = runner.invoke(app, ["crawl", "sympla", "--city", "linhares", "--dry-run"])
    assert result.exit_code == 2
