# Evidence Card — AgentOps Workbench

## Where this lives

`apps/agentops-workbench/` inside `sh-ai-x/WikiGrounded`.

## What we built

A LangGraph / FastAPI support-ops agent integrating:
- Custom document MCP server (stdio, spec `2026-07-28`)
- Pinned `@modelcontextprotocol/server-filesystem` (fixture- scope)
- 2 real tools wired to a run (`search_docs`, `read_document`, invoked by the
  `planner_executor` topology as of PR #21). `get_issue` has no backing data
  source anywhere in this codebase — a planned call to it normalizes to
  `MCPError(kind="unsupported_capability")` rather than faking a result.
  `create_ticket_draft` / `publish_ticket` are not tools at all: only
  `TicketLedger.publish()` exists, and `/v1/actions` mints a nonce without
  ever calling it. (Corrected 2026-09-13 — see `build-report.md`; previously
  claimed as "5 MVP tools", 2 real.)
- 3 execution topologies (fixed / single_agent / planner_executor) behind a single registry
- FastAPI surface with HS256 JWT (`POST /v1/runs`, `GET /v1/runs/{id}`, `POST /v1/runs/{id}/cancel`, `POST /v1/actions`)
- SQLAlchemy + Alembic migrations + SQLite (Postgres-ready)
- Async worker (concurrent.futures, production-swappable to arq+Redis)
- Streamlit UI skeleton
- OTel traces + credential redaction
- Mock ticket ledger (idempotent on action_key, rejects mutated args)
- 30-case benchmark (18 dev / 6 val / 6 held-out), 6 task families
- 3-prompt comparison harness (Phase 3 explicit requirement)
- Held-out experiment harness with SpendCeiling
- Docker Compose stack + operator runbook + 6 ADRs

## What we measured (LIVE — provider=minimax, model=MiniMax-M3)

### Held-out (24 runs)
- 6 cases x 2 trials x 2 topologies = 24 runs
- 9,074 total tokens (5,782 prompt + 3,292 completion)
- $0.0124 total cost
- 153.7s total duration; 6.40s avg per run
- Held-out SHA: `d3bef8c35a3bf9de539ab67a988e83a2d5434746bc8c6001f85ddf569dc8d0a5`

### 3-prompt comparison (18 runs)
- 6 cases x 3 prompt versions = 18 runs
- 14,597 total tokens (v1=3,519, v2=4,054, v3=6,024)
- v3_minimal emitted 2x more completion tokens (4,885 vs 2,272-2,315) — no system prompt means longer, less focused responses
- v3_minimal published as unsuccessful change per proposal

### Local deterministic gates
- **177 tests passing** (re-run 2026-09-13, `main` post-PR#20/#21; previously
  claimed 144), 5 pre-existing ruff findings remain in `scripts/*.py`
  (unrelated screenshot helper, out of scope for #20/#21)
- Held-out + 3-prompt experiments reproducible end-to-end via `python

m -m agentops_workbench.experiments.<held_out|prompts>`

## What we shipped

- Topology: **fixed graph** as the default per ADR-0006
- Provider: `minimax` (live, model `MiniMax-M3`) per ADR-0003 + `local-fake` (CI)
- 3-prompt selection: v1_baseline + v2_structured (v3_minimal published as unsuccessful change per proposal)
- Held-out set: 6 cases, content-hashed into `HELD_OUT_SHA256.txt`

## Limitations (per proposal §"Step-by-Step Build Guide")

- Held-out set is 6 cases (small by design; proposal explicitly labels this "illustrative, not statistically settled")
- Substring-match scorer is conservative; many runs report task_success=false even when the model's answer is semantically correct
- Topology comparison does not vary temperature / model / corpus (R4 invariant; only the topology changed)

## How to reproduce

```bash
cd apps/agentops-workbench
uv sync --extra dev
cp .env.example .env
# AGENTOPS_MINIMAX_API_KEY + AGENTOPS_MINIMAX_BASE_URL=https://api.minimax.io/v1
# AGENTOPS_MODEL=MiniMax-M3
AGENTOPS_PROVIDER=minimax uv run python -m agentops_workbench.experiments.run_held_out
AGENTOPS_PROVIDER=minimax uv run python -m agentops_workbench.experiments.prompts
```

## Resumé bullet (measured numbers)

> Built a LangGraph/FastAPI support agent integrating 2 MCP servers
> (document + filesystem); compared 2 workflow configurations and 3
> prompt versions on a 30-case benchmark; ran 42 live experiments
> (24 held-out + 18 prompt-comparison) on `MiniMax-M3` for $0.03 total
> cost and 177 passing tests. The held-out/prompt numbers above predate
> PR #20 (LangGraph `StateGraph` rewrite) and #21 (real `search_docs`/
> `read_document` tool execution in `planner_executor`) — they have not
> been re-run since; see `build-report.md` for current status.

## References

- Proposal: `../../docs/proposals/agentops-workbench-proposal.md`
- Plan: `../../../.dev-kit/round-1/{PRD.md, phases/build/step1..7.md}`
- ADRs: `docs/adr/0001..0006-*.md`
- Held-out artifacts (live): `experiments/held-out-v1/`
- 3-prompt comparison (live): `experiments/prompts-v1/`
