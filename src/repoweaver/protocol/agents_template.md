# RepoWeaver — AGENTS.md Injection Template

Copy the section below into the `AGENTS.md` (or `CLAUDE.md` / `CODEX.md`) at the
root of any repository indexed by RepoWeaver. It gives your AI coding agent the
three rules it needs to use the `explore()` MCP tool correctly.

---

<!-- repoweaver:start -->
## RepoWeaver Code-Context Rules

### 1. Freshness Gate

**Before starting any session**, run:

```bash
fabric check
```

If the output contains `STALE`, rebuild the index first:

```bash
fabric build
```

Never query an index that `fabric check` reports as stale — results will reflect
old code and may lead to incorrect conclusions.

### 2. Pointers, Not Answers

Results from `explore()` are **pointers into the source**, not ground truth.

- A hit in `slices` tells you *where to look*, not *what the code does*.
- Always verify the returned `file` + `span` by reading the actual source.
- Use `grep` or your editor to confirm call sites before acting on them.
- A confident edge (confidence ≥ 0.9) still requires human/agent source review
  before you treat it as definitive.

### 3. Incompleteness Contract

The graph is built from **static analysis only**. The following patterns are
**not represented**, even if they exist in the codebase:

| Not visible in graph | Why |
|---|---|
| Spring / CDI bean injection dispatch | Dynamic proxy beyond declared type |
| Message-queue listener call targets | Runtime binding (topic → handler) |
| Reflection (`Class.forName`, `Method.invoke`, …) | Resolved at runtime |
| Config-driven routing (e.g. `@ConditionalOnProperty`) | Condition unknown at parse time |
| Generated code (MyBatis `Example`, Lombok, APT, …) | Not present as source |

> **"No callers found" ≠ dead code.**
> The symbol may be invoked via any of the mechanisms above.
> Always verify with `grep`/source search before concluding that a symbol is unused.

---

*This template is maintained by RepoWeaver. Do not edit the three numbered sections
above; they reflect the current incompleteness contract of the static analyser.*
<!-- repoweaver:end -->
