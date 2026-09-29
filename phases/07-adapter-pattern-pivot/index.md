# 07-adapter-pattern-pivot

> Phase 7 — Evidence-source adapter generalization + OSS-maintainer pillars

- **Proposal section:** [`../../docs/proposals/agentops-workbench-proposal.md`](../../docs/proposals/agentops-workbench-proposal.md) §"Pivot (2026-09-13)" and §"Phase 7"
- **Canonical step plan:** none. The `.dev-kit/round-1/` planning round covered `step1..step7` = Phases 0–6 only; this phase is planned directly in the proposal and [ADR-0007](../../apps/agentops-workbench/docs/adr/0007-evidence-source-adapter-pattern.md), so there is no `step<N>.md` ancestor and no `step<N>-output.json`.
- **Estimated:** 2.0 weeks (plan value, not measured effort)
- **Exit criterion:** A run against a GitHub issue returns a draft whose every citation resolves to an `EvidenceRef` from a registered adapter, with the docs-corpus test suite passing unchanged and `retrieval_recall` reported per `source_kind`
- **Status:** **partially built** — PR #34 (merged) implements all
  five adapters (`WikiRagAdapter`, `GitHubIssueAdapter`,
  `SecurityLogAdapter`, `IncidentLogAdapter`, `TicketSystemAdapter`)
  against the Protocol below, 223 tests passing, ruff clean. Deliberately
  **not** wired into `graph/**` yet — see
  [ADR-0008](../../apps/agentops-workbench/docs/adr/0008-github-url-cli-and-deployment-target.md)
  and [phase 08](../08-deployable-mvp/index.md) for the flow that wires
  two of them in.
- **Build output:** [PR #34](https://github.com/sh-ai-x/WikiGrounded/pull/34)

## Why this phase exists

The portfolio narrative pivoted on 2026-09-13. The original
support-operations / ticketing framing is retired because the operator has
no domain expertise there and so cannot credibly judge answer quality — a
benchmark whose owner cannot grade its own gold labels is not evidence. It
is superseded by an open-source-maintainer / developer-tooling framing,
where the system's existing inputs (an issue, a searched corpus, a run's
own trace) are a natural fit.

The architecture does not change. **One seam generalizes:** the evidence
source. `DocumentClient` in `mcp/__init__.py` is already an adapter-shaped
Protocol that `graph/planner_executor.py` consumes by interface (PR #21,
merged), and PR #29 (open) adds `SubprocessDocumentClient` as a second
real implementation. This phase names that pattern
(`EvidenceSourceAdapter`) and adds siblings for non-document sources.

## Deliverables — as planned vs. as built (PR #34, reconciled 2026-09-13)

This section originally planned a narrower, sequenced rollout. PR #34
built a wider slice in one pass instead. Recorded honestly rather than
quietly rewritten to look predicted:

1. **Rename** — done, but not as planned. Planned: `DocumentClient` kept
   as a deprecated alias, `list_filesystem_files` moved to a
   `FilesystemScopedSource` extension Protocol. Built: `DocumentClient`
   is untouched and still lives at `mcp/__init__.py` (no alias — the new
   `EvidenceSourceAdapter` Protocol in `adapters/base.py` is additive, not
   a replacement, specifically so it doesn't conflict with #21/#25/#26/#29
   which all still call `DocumentClient` directly); `list_filesystem_files`
   was **dropped**, not extension-Protocol'd — real defect found:
   `SubprocessDocumentClient` (#29) already couldn't implement it, so a
   shared method one of two implementations can't honour was never
   actually shared.
2. **`EVIDENCE_SOURCES` registry** — **not built**. Each adapter is
   constructed directly; no `TOPOLOGIES`-style registry or
   `AGENTOPS_EVIDENCE_SOURCES` config exists yet. Genuinely open, unlike
   the items below.
3. **`IncidentLogAdapter`** — done, roughly as planned. Aggregates
   `ToolCall`-shaped error records by `error_kind` within a window into
   one `EvidenceRef` per aggregate.
4. **`GitHubIssueAdapter`** — done. Real `httpx` calls against the GitHub
   REST API; tests use `pytest-httpx`, zero live network calls (narrower
   than the original "a marked live integration test" plan — no live-
   network test exists).
5. **Observability/eval layer (Pillar 2)** — **not built** in this phase.
   Still substrate-only (`Usage`/`ToolCall`, merged in #21).
6. **`WikiRagAdapter`** — built, **despite this section originally saying
   "explicitly not in this phase."** The stated blocker ("carries a
   pgvector reversal that needs its own ADR") turned out not to apply:
   it's real, pure-Python TF-IDF/cosine similarity, no embeddings, no
   vector store, no pgvector decision to make.
7. **`SecurityLogAdapter`** — built, despite being "deferred" here.
   Structured JSONL, `window`/`filters`-driven, matching
   `IncidentLogAdapter`'s query shape as this section predicted.
8. **`TicketSystemAdapter`** — built. Not listed in the original plan at
   all — a thin facade over the existing `TicketLedger`, the customer-
   support/ticketing extensibility proof (zero new domain logic, added
   two small additive read-only methods to `mocks/tickets.py`).

Net: 5 of 5 adapters exist and are tested (223 passing); the registry/
config layer and the eval layer are the genuinely remaining gaps, not the
adapters themselves. See
[phase 08](../08-deployable-mvp/index.md) for what wires two of the five
into an actual flow.

## What already exists (do not re-claim as new)

Pillar 2's substrate is shipped. Naming it accurately is part of this
phase's honesty requirement:

- `llm/adapter.py:Usage` — per-call `prompt_tokens`, `completion_tokens`,
  `total_tokens`, `cost_usd`, `provider`, `model`.
- `db/models.py:ToolCall` — persisted `{tool_name, policy_decision,
  action_key, args_canonical, outcome, latency_ms}` with args-based
  idempotency keys.
- `graph/planner_executor.py:_execute_node` — per-call `{tool_name,
  outcome, latency_ms, error_kind}` into `tool_results` (PR #21 merged;
  PR #25 open with review fixes; PR #26 open, extends it to
  `single_agent`).
- `classify_mcp_error` — `timeout / disconnect / malformed /
  unsupported_capability / permission_denied` buckets.
- OpenTelemetry span export; the frozen-manifest experiment runner.

The **new** work is the quality/drift layer on top, not the
instrumentation.

## Honest cost

ADR-0007 §Consequences states this in full. Summary: the pattern is cheap,
the adapters are four separate retrieval projects. `WikiRagAdapter` needs
embeddings, a vector index and its own recall gold labels;
`GitHubIssueAdapter` inherits GitHub's own rate limits and is the only
adapter reading live third-party mutable state; the two log adapters are
not lexical search at all but `(time window, predicate, aggregation)`. The
most underestimated cost is answer-shape divergence: a log citation is an
*aggregate* ("47 429s between 04:00 and 04:20"), not a quotation, and both
the synthesis prompt and the groundedness scorer must handle that.

Per-source consequences that bind other decisions:

- **ADR-0006 is scoped to `DocsCorpusAdapter`.** Its fixed-graph selection
  was made over the synthetic corpus with stubbed tools. `graph/fixed.py`
  reads `fixtures/docs/` directly and cannot consult a GitHub issue at
  all, so the topology choice must be re-measured per source.
- **ADR-0004's frozen 18/6/6 split is per source.** Each adapter needs its
  own reviewed cases and its own held-out freeze; numbers do not pool, and
  the existing held-out SHA does not cover them.
- **`EVIDENCE_CARD.md` counts stay per source.** [`../build-report.md`](../build-report.md)
  already records that file overstating a count; the same mistake across
  five sources would be worse.

## Relationship to open PRs (as of 2026-09-13)

| PR | State | Bearing on this phase |
|----|-------|-----------------------|
| #20, #21 | merged | `StateGraph` rewrite; real MCP tool execution in `planner_executor` + `tool_results`. The substrate Pillars 1 and 2 build on. |
| #24 | open | Reconciles phase status docs with #20/#21's actual scope. Overlaps `phases/**`; expect a textual conflict, not a semantic one. |
| #25 | open | Review fixes on #21's `planner_executor`. Deliverable 1 must land after it. |
| #26 | open | Extends real tool execution to `single_agent`. Widens the adapter's caller set. |
| #27 | open | Held-out runner excluded `planner_executor` and never scored real `tool_correctness`. Must land before any per-source metric is quoted. |
| #29 | open | `SubprocessDocumentClient` — the second real implementation of the Protocol, and prior art for the whole pattern. Conflicts with deliverable 1 **by rename only**; its `NotImplementedError` for `list_filesystem_files` disappears. |
| #30 | open | Wires `POST /v1/actions/{id}/execute` → `TicketLedger.publish()`. Pillar 1's draft-publish path. |
| #31 | open | `main_stdio()` default `corpus_dir` off by one `.parent` — affects `DocsCorpusAdapter`'s subprocess flavour. |

## Related prior art (not the same initiative)

Repo issue [#22](https://github.com/sh-ai-x/WikiGrounded/issues/22)
proposes `claim_fidelity` — `claim.made` / `claim.audit` events and a
`false_positive_rate = refuted / audited` reducer — for **dev-kit's own
harness effectiveness**: whether a *build agent's* completion claims about
the harness are true. It shares the independent-verification mechanism
with Pillar 2 and nothing else. Different event stream, different subject,
different consumer. Not merged into this phase; neither one's scope or
numbers transfer to the other.

## Cross-references

- Source proposal: [`../../docs/proposals/agentops-workbench-proposal.md`](../../docs/proposals/agentops-workbench-proposal.md)
- Governing ADR: [`../../apps/agentops-workbench/docs/adr/0007-evidence-source-adapter-pattern.md`](../../apps/agentops-workbench/docs/adr/0007-evidence-source-adapter-pattern.md)
- Seam being generalized: [`../../apps/agentops-workbench/src/agentops_workbench/mcp/__init__.py`](../../apps/agentops-workbench/src/agentops_workbench/mcp/__init__.py)
- Registry idiom mirrored: [`../../apps/agentops-workbench/src/agentops_workbench/graph/topology.py`](../../apps/agentops-workbench/src/agentops_workbench/graph/topology.py)
- Audit this phase must not repeat: [`../build-report.md`](../build-report.md)
- Root phase index: [`../index.md`](../index.md)
