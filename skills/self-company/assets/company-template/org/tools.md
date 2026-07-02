# Company Tools Registry

> **Owner: Tom (IT/Ops).** The company's employees do their jobs with tools — MCP
> servers and skills — not only built-in file/shell tools. This registry is the
> single source of truth for *what tools exist, who may use them, and their
> status*. Tom maintains it (inventory + health); July grants per-worker access;
> Phoebe dispatches work that uses them. Adding/removing a tool is a skeleton
> change → goes through the upgrade loop (and the dev-repo skeleton guard).

---

## How employees use tools

Per `references/execution-model.md`, the four workers run as **isolated
sub-agents**. When Phoebe dispatches a task, the worker is granted ONLY the tools
its role needs (least privilege) — its built-in tools (Read/Edit/Bash/…) **plus**
any MCP tools or skills listed for it below. A worker does not get the whole tool
surface; it gets its slice, the same way it gets its memory slice.

- **MCP tools** are surfaced by name and called like any other tool (some load
  on demand via tool-search). The granting agent passes the worker the MCP tools
  relevant to the task.
- **Skills** are invoked via the Skill mechanism; a worker may use a skill listed
  for its role when the task calls for it.

---

## Registry

| Tool | Type | Owner/Manager | Who may use it | Purpose | Status |
|---|---|---|---|---|---|
| Playwright | MCP | Tom | Gibby | Browser drive / screenshot / UI verification | available (per environment) |
| RAG (rag_query) | local script | Tony | Tony, Gibby | Semantic memory search (fastembed + LanceDB) | active when `rag_setup.sh install` has run |
| deep-research | skill | Tom | Tony (research), Bob (spec research) | Multi-source web research with verification | available per environment |
| GitHub (`gh`) | CLI | Tom | Tom (PRs/merges/issues on Chairman approval) | Create/manage pull requests, releases, issues for the skill repo | installed; needs `gh auth login` (no token by default) |
| *(add as needed)* | | Tom | | | |

> Keep this table honest: an entry here means the tool is actually reachable in
> this environment. Tom verifies on the weekly/daily infra check and marks status
> `available` / `degraded` / `absent`. A tool that isn't reachable degrades
> gracefully — the worker falls back to built-in tools and notes it.

---

## Tom's responsibilities for tools

1. **Inventory** — keep this table current: what MCP servers / skills are
   connected, their names, and which roles use them.
2. **Health** — on the daily/weekly infra check, confirm each tool is reachable;
   mark degraded/absent ones and alert Phoebe/Elon if a depended-on tool is down.
3. **Provisioning** — when an approved upgrade adds a tool (e.g. RAG via
   `rag_setup.sh`), Tom installs it and records it here.
4. **Least privilege** — with July, ensure each worker's `context.md` tool grants
   match this registry (no worker holds tools it doesn't need).

---

Version: v1 (2026-06-29) — initial tools registry (Tom-owned).
