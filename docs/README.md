# Docs

| Folder | Holds |
|---|---|
| [`design/`](design/) | **How** things are built: architecture and migration plans. Start with [engine-split.md](design/engine-split.md) |
| [`proposals/`](proposals/README.md) | **What** could be built next: one improvement idea per file, with problem, protocol additions, fit in the plan, value and effort |

## Conventions

- **Proposal numbering:** `NNNN-kebab-title.md`, four digits, sequential, never reused. Copy [`proposals/_template.md`](proposals/_template.md) and add a row to [`proposals/README.md`](proposals/README.md).
- **Proposal status:** `Proposed` → `Accepted` (planned) → `Done` (shipped, link the PR); or `Rejected` (keep the file, add why).
- **Design docs** cite code as `file:line` under `src/lazyagent/` at the commit named in the doc header.
