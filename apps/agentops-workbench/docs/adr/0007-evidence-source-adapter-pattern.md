# ADR-0007: Evidence-source adapter pattern

## Status

Accepted (2026-09-13). Narrows the single-corpus assumption in
[ADR-0002](0002-mcp-boundaries.md) and scopes [ADR-0006](0006-topology.md)'s
selection to one evidence source; ADR-0002's transport and fixture-scope
rules remain in force for the document server itself.

## Context

The agent graph reaches evidence through exactly one seam —
`DocumentClient` in `src/agentops_workbench/mcp/__init__.py`:

```python
class DocumentClient(Protocol):
    def search_docs(self, query: str, top_k: int = 5) -> list[DocRef]: ...
    def read_document(self, doc_id: str, offset: int = 0, limit: int = 2000) -> str: ...
    def list_filesystem_files(self) -> list[str]: ...
```

Two implementations exist. `InMemoryDocumentClient` reads
`fixtures/docs/*.md` in-process. `SubprocessDocumentClient` (PR #29,
**open**) launches the document MCP server as a real subprocess and speaks
JSON-RPC over its pipes. `graph/planner_executor.py` (PR #21, **merged**)
receives the Protocol — not a concrete class — via
`state["document_client"]`, so the graph is already written against the
interface rather than an implementation.

The substitutability this ADR is about therefore **already exists in
miniature**, for one kind of evidence. What does not exist is a name for
it, a stated contract, or a second *kind* of source behind it. Both
current implementations back the same synthetic Markdown corpus, and the
Protocol's method names bake that corpus into the interface.

The portfolio pivot recorded in
[`docs/proposals/agentops-workbench-proposal.md`](../../../../docs/proposals/agentops-workbench-proposal.md)
§"Pivot (2026-09-13)" retires the support-operations framing in favour of
open-source maintainer tooling. That reframing needs the evidence layer to
accept a GitHub issue thread, an internal/personal wiki, a security log
and an AI-incident log — sources that are not a directory of Markdown
files and, in two of four cases, are not lexically searchable documents at
all.

## Decision

### 1. Name the seam `EvidenceSourceAdapter`

Promote the `DocumentClient` Protocol to a named, documented pattern:
**one `EvidenceSourceAdapter` Protocol, N per-deployment implementations,
selected by configuration**. This mirrors the two registries the project
already ships — `TOPOLOGIES` in `graph/topology.py` and the
`provider ∈ {openai, minimax, local-fake}` allow-list from
[ADR-0003](0003-provider-abstraction.md) — so there is one idiom for
"swappable implementation behind a typed interface" instead of three
ad-hoc ones.

The name changes because the interface's subject changes. A GitHub issue,
a log window and a wiki page are all *evidence*; only one of them is a
*document*. `DocumentClient` is kept as a deprecated alias for one
release so PR #29 and the merged `planner_executor` work need not be
rebased around a rename.

### 2. Rename `search_docs` / `read_document`, and widen the signature

This is a deliberate breaking rename, not incidental tidying. The
existing names are too document-corpus-specific to survive a log source,
and the reasons are recorded here rather than left implicit in a diff:

| Before | After | Why |
|---|---|---|
| `search_docs(query, top_k)` | `search_evidence(query, top_k, *, window=None, filters=None)` | A log adapter cannot answer a bare lexical query. It needs a time window ("the last 6h of 429s") and structured predicates (`status_code`, `model`, `principal`). Smuggling those inside `query` would force every adapter to re-parse a private micro-syntax. |
| `read_document(doc_id, offset, limit)` | `read_evidence(evidence_id, offset, limit)` | `doc_id` is a corpus filename stem today. The generalized id is opaque and adapter-minted (`gh:owner/repo#1234`, `wiki:notes/langgraph-checkpoints`, `sec:2026-09-13T04:11:02Z/auth-deny/7f2a`). Callers must not parse it. |
| `list_filesystem_files()` | **removed from the Protocol** | It was never part of this abstraction — it describes the pinned `@modelcontextprotocol/server-filesystem` and nothing else, which is exactly why `SubprocessDocumentClient` (PR #29) must raise `NotImplementedError` for it. A method one of two existing implementations cannot honour is not a shared contract. It moves to an optional `FilesystemScopedSource` extension Protocol that only the filesystem-backed adapter satisfies. |

`DocRef` becomes `EvidenceRef` and gains the fields non-document sources
need in order to be citable at all:

```
EvidenceRef: evidence_id, title, score, source_kind, uri, retrieved_at
```

`source_kind` is what an answer's citation renders (`"docs-corpus"`,
`"wiki"`, `"github-issue"`, `"security-log"`, `"incident-log"`) and what
the groundedness scorer groups by. `retrieved_at` exists because two of
the four new sources are mutable: an issue gets edited, a log window
slides. A citation without a retrieval timestamp is not reproducible, and
reproducibility is this project's central claim.

### 3. Adapters this pivot targets

| Adapter | Backs | Retrieval strategy | Status |
|---|---|---|---|
| `DocsCorpusAdapter` | `fixtures/docs/*.md` (today's `InMemoryDocumentClient`; `SubprocessDocumentClient` per PR #29) | lexical term-count — the proposal's "Lexical baseline" row, per ADR-0002 | **exists** — rename only |
| `IncidentLogAdapter` | AI-incident records: timeouts, rate limits (429), provider errors, budget cuts | time-windowed aggregate over persisted `ToolCall` / `Usage` rows | substrate exists, adapter not started |
| `GitHubIssueAdapter` | GitHub Issues / Discussions on a target repo | GitHub Search + REST/GraphQL; body, labels, comment thread | not started |
| `WikiRagAdapter` | internal / personal wiki (MyWiki) | chunk + embed + ANN retrieval | not started |
| `SecurityLogAdapter` | security / authorization logs | time-windowed structured query, not free-text search | not started |

`IncidentLogAdapter` is listed second deliberately: its data already
exists in this repo. `db/models.py:ToolCall` persists
`{tool_name, policy_decision, action_key, args_canonical, outcome,
latency_ms}`, `graph/planner_executor.py:_execute_node` records
`{tool_name, outcome, latency_ms, error_kind}` per call (PR #21, merged),
`llm/adapter.py:Usage` carries per-call tokens and `cost_usd`, and
`classify_mcp_error` already buckets failures into
`timeout / disconnect / malformed / unsupported_capability /
permission_denied`. An incident adapter reads its own project's
operational history — the smallest honest end-to-end demonstration of the
pattern, needing no new credential and no new infrastructure.

### 4. Selection is configuration, not code

Deployments declare sources the way they already declare a provider:

```
AGENTOPS_EVIDENCE_SOURCES=github_issue,wiki_rag
```

Order is retrieval priority. Unknown names fail at startup, as the
provider allow-list already does. `local-fake` keeps its ADR-0003
meaning: under CI every configured name resolves to a deterministic fake
adapter, so no test touches the network.

### 5. Authorization stays outside the adapter

The proposal's §"Auth and Identity" rule is unchanged, but it becomes
load-bearing in a way it was not with a single read-only fixture corpus.
An adapter never chooses a `principal_id`, and scope is checked
immediately before each `search_evidence` / `read_evidence` call, not at
plan time. An adapter is a retrieval mechanism, not a policy decision
point. `is_within_scope` generalizes from "is this path under
`fixtures/docs/`" to a per-adapter scope predicate — which repos, which
wiki namespaces, which log streams — and every adapter ships a negative
test proving an out-of-scope request is refused, the same obligation
ADR-0002 placed on the filesystem server.

## Consequences

### Honest cost: this is four retrieval projects, not one interface

The pattern is cheap. The adapters are not. Nothing here is "swap a
string", and the ADR should not be quoted as if it were:

- **`WikiRagAdapter`** needs chunking, an embedding model, a vector index
  and a recall evaluation of its own. It is the first non-lexical
  retrieval in the project, which means `retrieval_recall@k` needs gold
  labels over wiki content before any number from it means anything. It
  also pulls pgvector back into scope, which the proposal explicitly
  deferred — that reversal needs its own ADR, not a footnote here.
- **`GitHubIssueAdapter`** inherits GitHub's own rate limits, a second
  independent rate-limit surface on top of the LLM provider's. It needs
  pagination, comment-thread assembly, and it is the only adapter reading
  live third-party mutable state. Recorded HTTP fixtures are required for
  CI; live calls belong in a marked integration test.
- **`SecurityLogAdapter`** and **`IncidentLogAdapter`** are not search at
  all. Their query model is `(time window, structured predicate,
  aggregation)`. Forcing them through a bare `query: str` would repeat
  `search_docs`'s mistake one level up — hence the `window` / `filters`
  widening in §2 rather than a straight rename.
- **Answer-shape divergence.** A grounded answer over a wiki cites a
  passage. A grounded answer over a log cites *a count over a window*
  ("47 `429`s from `MiniMax-M3` between 04:00 and 04:20"). Both the
  synthesis prompt and the groundedness scorer have to handle a citation
  that is an aggregate rather than a quotation. This is the most
  underestimated cost in the pivot, and it is not addressed by the
  Protocol change alone.

### Effects on existing work

- **Breaking rename.** `search_docs` → `search_evidence`,
  `read_document` → `read_evidence`, `DocRef` → `EvidenceRef`. Every call
  site in `graph/**`, `mcp/**` and `tests/**` moves. See §Migration path.
- **PR #29 conflicts by rename only.** `SubprocessDocumentClient`'s
  design is correct and unaffected; three method names and one return
  type change. Its `NotImplementedError` for `list_filesystem_files`
  disappears entirely — which is itself evidence that the §2 removal is
  the right call.
- **The topology comparison gains a second axis.** ADR-0006 selected the
  fixed graph under one corpus. Adapters make (topology × source) the
  real experiment space, and the fixed graph's win probably does not
  transfer: `graph/fixed.py` retrieves by reading `fixtures/docs/`
  directly, so it has no mechanism to consult a GitHub issue at all.
  **ADR-0006's decision is hereby scoped to `DocsCorpusAdapter`** and
  must be re-run per source before it is quoted for any other.
- **Dataset splits are per source.** [ADR-0004](0004-dataset-separation.md)'s
  frozen 18/6/6 split is over the synthetic corpus. Each new adapter needs
  its own reviewed cases and its own held-out freeze. Numbers do not pool
  across sources, and the existing held-out SHA does not cover them.
- **`EVIDENCE_CARD.md` claims stay per source.** Its tool and case counts
  are `DocsCorpusAdapter` numbers. Adding adapters must not silently
  inflate them — [`phases/build-report.md`](../../../../phases/build-report.md)
  already records that file overstating a count, and the same mistake is
  easier to make, and worse, across five sources.

### What stays the same

LangGraph topologies, the `MAX_STEPS=8` budget cap, the FastAPI surface,
approval / nonce / action-key idempotency, OpenTelemetry export, the
benchmark harness and the `LLMAdapter` provider abstraction are all
untouched. This ADR generalizes one seam. It is explicitly not an
architecture rewrite.

## Alternatives considered

**A. Keep one `DocumentClient`; pre-flatten every source into the
Markdown corpus.** An ingest job renders issues and log windows to `.md`,
and nothing in the graph changes. By far the cheapest, and it keeps
lexical retrieval honest. *Rejected*: every source becomes stale by the
ingest interval, which is fatal for logs and incidents (the interesting
query is "the last 20 minutes"); flattening to prose discards the
structure that matters (`error_kind`, labels, status codes); and it hides
the per-source engineering the pivot exists to demonstrate behind a cron
job.

**B. One MCP server per source; no in-process adapter Protocol.** Each
source becomes a subprocess MCP server and the "adapter" is MCP's own
tool discovery. Genuinely attractive — protocol-native, and PR #29 proves
the subprocess machinery works. *Rejected as the primary mechanism*: it
forces a subprocess per source per run, and PR #29's own scope note flags
subprocess-per-run latency, pooling and lifecycle as an unsolved
decision. It is also orthogonal — a source can be implemented in-process
*or* as an MCP server behind the same adapter. Adopted as a per-adapter
implementation option, not as the abstraction.

**C. LangChain `BaseRetriever` as the interface.** Free ecosystem
adapters. *Rejected*: it returns `Document` objects, i.e. the exact
document-shaped assumption being escaped here, with no natural home for
`window`, `filters`, `source_kind` or `retrieved_at`; and it would put a
LangChain type into graph state, which ADR-0003's "no provider-specific
code paths in the agent graph" rule is the standing precedent against.
Individual adapters may wrap LangChain retrievers internally.

**D. One fat adapter with a `source_kind` switch.** One class, four code
paths. *Rejected*: it is the shape the `Protocol` already replaced, and it
makes per-source testing, per-source scope enforcement and per-source
dataset freezing impossible to isolate.

## Migration path

Staged so that no single PR both renames and adds behaviour.

1. **Rename, no new sources.** Introduce `EvidenceSourceAdapter`,
   `EvidenceRef`, `search_evidence`, `read_evidence`; move
   `list_filesystem_files` to `FilesystemScopedSource`; keep
   `DocumentClient` as a deprecated alias with thin `search_docs` /
   `read_document` shims. Pure refactor — the test suite must pass with an
   unchanged count. Lands **after** PRs #25–#31 so it does not
   rebase-thrash open work.
2. **Registry + config.** An `EVIDENCE_SOURCES` registry mirroring
   `TOPOLOGIES`, `AGENTOPS_EVIDENCE_SOURCES` parsing, startup validation,
   `local-fake` resolution for CI. `DocsCorpusAdapter` is the only
   registered entry; behaviour is byte-identical to today.
3. **`IncidentLogAdapter`.** First genuinely new source, reading this
   project's own `ToolCall` / `Usage` rows. Proves the `window` /
   `filters` widening against a real non-document source with no external
   credential.
4. **`GitHubIssueAdapter`.** The OSS-maintainer story's primary input.
   Recorded-fixture CI, a marked live integration test, explicit
   rate-limit handling, a per-repo scope predicate plus its negative test.
5. **`WikiRagAdapter`.** Largest: embeddings, vector index, its own recall
   gold labels. Carries the pgvector reversal, which needs its own ADR.
6. **`SecurityLogAdapter`.** Deferred; shares `IncidentLogAdapter`'s
   time-windowed query shape, so it is largely a second instance of a
   proven mechanism.
7. **Drop the deprecated aliases** once 1–4 have landed and no caller uses
   them.

Step 1 gates step 2. Steps 3–6 are independent of each other and may be
reordered by whatever the portfolio narrative needs next.

## Related prior art (not the same initiative)

Repo issue **#22** ("claim-fidelity measurement for long-running task
completion claims") proposes `claim.made` / `claim.audit` event types and
a `false_positive_rate = refuted / audited` reducer over dev-kit's
`.dev-kit/trace/**/*.jsonl`. It shares a *mechanism* with the
observability pillar this pivot sets up: independent verification, where
an audit must not be self-graded.

It is a **different subject**. Issue #22 audits whether a *build agent's
completion claims about the harness* are true. The evidence adapters here,
and the eval work built on them, audit whether *this agent's answers to
users* are grounded and at what cost. Different event stream, different
subject, different consumer. They are not merged, and neither one's scope
should be quoted for the other.

## References

- `src/agentops_workbench/mcp/__init__.py` — the `DocumentClient` Protocol being generalized.
- `src/agentops_workbench/graph/topology.py` — the `TOPOLOGIES` registry this pattern mirrors.
- [ADR-0002](0002-mcp-boundaries.md) — MCP transport and fixture-only scope (still in force).
- [ADR-0003](0003-provider-abstraction.md) — the provider allow-list idiom reused here.
- [ADR-0004](0004-dataset-separation.md) — the frozen split that now needs a per-source instance.
- [ADR-0006](0006-topology.md) — topology selection, now scoped to `DocsCorpusAdapter`.
- PR #21 (merged) — `planner_executor` takes `DocumentClient` via graph state; per-call `tool_results`.
- PR #29 (open) — `SubprocessDocumentClient`, the second real implementation of the same Protocol.
- [`docs/proposals/agentops-workbench-proposal.md`](../../../../docs/proposals/agentops-workbench-proposal.md) §"Pivot (2026-09-13)" — the portfolio narrative this ADR serves.
- [`phases/07-adapter-pattern-pivot/index.md`](../../../../phases/07-adapter-pattern-pivot/index.md) — the phase that executes this migration path.
