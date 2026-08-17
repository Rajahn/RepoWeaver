import repoweaver
from typer.testing import CliRunner
from repoweaver.cli import app


def test_version():
    assert repoweaver.__version__ == "0.0.1-dev"


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.0.1-dev" in result.output


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
