"""RepoWeaver protocol package — AGENTS.md injection templates and contracts."""

from __future__ import annotations

from pathlib import Path

_START_MARKER = "<!-- repoweaver:start -->"
_END_MARKER = "<!-- repoweaver:end -->"
_TEMPLATE_PATH = Path(__file__).parent / "agents_template.md"


def _extract_block(template_text: str) -> str:
    start = template_text.index(_START_MARKER)
    end = template_text.index(_END_MARKER) + len(_END_MARKER)
    return template_text[start:end]


def inject_agents_md(target_path: str | Path) -> bool:
    """Idempotently inject the RepoWeaver rules block into an AGENTS.md file.

    Content outside the ``<!-- repoweaver:start -->``/``<!-- repoweaver:end -->``
    markers is never touched. Re-running this on a file that already has the
    block replaces only that block (refreshing it to the current template),
    so repeated ``fabric init`` calls are safe.

    Returns True if the file was created or changed, False if already up to date.
    """
    target = Path(target_path)
    block = _extract_block(_TEMPLATE_PATH.read_text(encoding="utf-8"))

    if not target.exists():
        target.write_text(block + "\n", encoding="utf-8")
        return True

    existing = target.read_text(encoding="utf-8")
    if _START_MARKER in existing and _END_MARKER in existing:
        start = existing.index(_START_MARKER)
        end = existing.index(_END_MARKER) + len(_END_MARKER)
        new_content = existing[:start] + block + existing[end:]
    else:
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
        if existing and not existing.endswith("\n"):
            sep = "\n" + sep
        new_content = existing + sep + block + "\n"

    if new_content == existing:
        return False
    target.write_text(new_content, encoding="utf-8")
    return True
