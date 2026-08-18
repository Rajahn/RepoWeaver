from __future__ import annotations

from typer.testing import CliRunner

from repoweaver.cli import app

runner = CliRunner()


def test_build_indexes_fixture(javademo_repo):
    result = runner.invoke(app, ["build", str(javademo_repo)])
    assert result.exit_code == 0, result.output
    assert "node(s)" in result.output
    assert (javademo_repo / ".repoweaver" / "graph.db").exists()


def test_check_ok_after_build(javademo_repo):
    runner.invoke(app, ["build", str(javademo_repo)])
    result = runner.invoke(app, ["check", str(javademo_repo)])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_check_stale_without_build(javademo_repo):
    result = runner.invoke(app, ["check", str(javademo_repo)])
    assert result.exit_code == 1
    assert "STALE" in result.output


def test_check_stale_after_edit(javademo_repo):
    runner.invoke(app, ["build", str(javademo_repo)])
    target = javademo_repo / "com/example/demo/App.java"
    target.write_text(target.read_text() + "\n// edit\n")
    result = runner.invoke(app, ["check", str(javademo_repo)])
    assert result.exit_code == 1
    assert "STALE" in result.output


def test_init_creates_workspace_and_agents_md(javademo_repo):
    result = runner.invoke(app, ["init", str(javademo_repo)])
    assert result.exit_code == 0, result.output
    assert (javademo_repo / ".repoweaver").is_dir()
    assert (javademo_repo / "AGENTS.md").exists()


def test_init_is_idempotent(javademo_repo):
    runner.invoke(app, ["init", str(javademo_repo)])
    first = (javademo_repo / "AGENTS.md").read_text()
    result = runner.invoke(app, ["init", str(javademo_repo)])
    assert result.exit_code == 0
    assert (javademo_repo / "AGENTS.md").read_text() == first


def test_verify_m1_passes_on_bundled_fixture():
    result = runner.invoke(app, ["verify", "--level", "m1"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.3.1" in result.output
