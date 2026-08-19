from __future__ import annotations

from typer.testing import CliRunner

from codecontextfabric.cli import app

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
    assert "0.5.1" in result.output


def test_console_entry_point_main_function_exists_and_builds(tmp_path):
    """Regression guard for the rename-era bug: [project.scripts] must point at
    a *callable function* (main), not the typer app object. CliRunner(app) tests
    the object but never exercises the console entry. This calls main() directly
    and confirms it dispatches to a real command."""
    import subprocess
    import sys

    java = tmp_path / "A.java"
    java.write_text("package p;\npublic class A { public void m() {} }\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.argv=['ccf','build',sys.argv[1]]; "
                "from codecontextfabric.cli import main; main()"
            ),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".repoweaver" / "graph.db").exists()
