from typer.testing import CliRunner

import codecontextfabric
from codecontextfabric.cli import app


def test_version():
    assert codecontextfabric.__version__ == "0.5.1"


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.5.1" in result.output


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
