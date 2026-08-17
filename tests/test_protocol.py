from __future__ import annotations

from repoweaver.protocol import inject_agents_md


def test_inject_creates_new_file(tmp_path):
    target = tmp_path / "AGENTS.md"
    changed = inject_agents_md(target)
    assert changed
    text = target.read_text()
    assert "<!-- repoweaver:start -->" in text
    assert "<!-- repoweaver:end -->" in text
    assert "RepoWeaver Code-Context Rules" in text


def test_inject_is_idempotent(tmp_path):
    target = tmp_path / "AGENTS.md"
    inject_agents_md(target)
    first = target.read_text()
    changed_again = inject_agents_md(target)
    assert not changed_again
    assert target.read_text() == first


def test_inject_preserves_user_content_outside_markers(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My Project Rules\n\nAlways run tests before committing.\n")
    inject_agents_md(target)
    text = target.read_text()
    assert "# My Project Rules" in text
    assert "Always run tests before committing." in text
    assert "<!-- repoweaver:start -->" in text


def test_reinject_replaces_only_the_block(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text(
        "# My Rules\n\n<!-- repoweaver:start -->\nSTALE OLD CONTENT\n<!-- repoweaver:end -->\n\n# Footer\n"
    )
    inject_agents_md(target)
    text = target.read_text()
    assert "STALE OLD CONTENT" not in text
    assert "RepoWeaver Code-Context Rules" in text
    assert "# My Rules" in text
    assert "# Footer" in text
