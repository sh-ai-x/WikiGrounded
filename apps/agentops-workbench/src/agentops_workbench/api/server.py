"""FastAPI surface — JWT auth + run creation/status/cancel + action approval.

Endpoints (proposal §"Contracts and APIs"):
  POST /v1/runs                   -> create run, returns {id, state: 'queued'}
  GET  /v1/runs/{id}              -> state, evidence, last error, token usage
  POST /v1/runs/{id}/cancel       -> state -> cancelling; refuses new tool calls
  POST /v1/actions/{id}/approve   -> action-bound approval
  POST /v1/wiki/index-files       -> upload .md files, returns corpus_id (AC1)
  GET  /v1/wiki/search            -> TF-IDF search over a corpus, with
                                     AC3 trust fields (source_path,
                                     evidence_span, coverage, etc.) (AC2/AC3)
  POST /v1/wiki/qa                -> search + LLM answer + per-sentence
                                     groundedness (AC3)

Auth: HS256 JWT (dev secret in .env). principal_id from the `sub` claim.
"""
from __future__ import annotations

import hashlib
import html as _html
import logging
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import jwt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .. import dev_metrics, oss_helper, wiki_corpus
from ..db.models import Action, Run, ToolCall
from ..db.session import session_scope
from ..graph.state import RunState, is_terminal, make_action_key
from ..graph.topology import run_topology
from ..graph.wiki_chat import run_wiki_chat
from ..llm.errors import LLMProviderError
from ..llm.factory import make_adapter
from ..mocks.tickets import TicketLedger
from ..settings import get_settings
from ..wiki_metrics import (
    GroundednessRecorder,
    GroundednessSample,
    StageLatencyRecorder,
    StageSample,
)

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Refuse to serve with a forgeable JWT secret outside the fake provider."""
    settings = get_settings()
    if settings.has_insecure_jwt_secret() and settings.provider != "local-fake":
        raise RuntimeError(
            "AGENTOPS_JWT_SECRET is the dev default or shorter than 32 chars while "
            f"provider={settings.provider!r}. Set a strong secret before starting the "
            "API: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    yield


app = FastAPI(title="AgentOps Workbench API", version="0.2.0", lifespan=_lifespan)

# Module-level metrics aggregators. Singleton pattern matches the wiki
# corpus registry -- process-local, lifetime = process lifetime. Tests
# reset via `reset_for_tests()`. The 200-sample trailing window is small
# enough that p50/p95 stays cheap to compute on every dashboard poll,
# and large enough that the numbers don't jitter every refresh.
search_metrics = StageLatencyRecorder(window=200)
groundedness_metrics = GroundednessRecorder(window=200)

# Single ledger instance per process. Step 6 wires DI properly.
_ledger: TicketLedger | None = None


def get_ledger() -> TicketLedger:
    global _ledger
    if _ledger is None:
        _ledger = TicketLedger()
    return _ledger


# ---- Schemas ----


class CreateRunBody(BaseModel):
    task: str
    graph_version: str = "fixed-v1"
    prompt_version: str = "v1_baseline"
    model_config: dict[str, Any] = {}
    budget: dict[str, Any] = {}
    # Per-run corpus override. When set, takes precedence over both
    # wiki_dir and docs_dir; wiki_mode stays False (treated as a plain
    # docs_dir override, not a wiki). Empty string means "use server
    # default" (= wiki_dir if set, else docs_dir).
    corpus_dir: str = ""


# Maps the API-facing graph_version string to a topology.TOPOLOGIES key.
# README documents these three graph_version strings; keep both in sync.
GRAPH_VERSION_TO_TOPOLOGY: dict[str, str] = {
    "fixed-v1": "fixed",
    "single-agent-v1": "single_agent",
    "planner-executor-v1": "planner_executor",
}


class RunView(BaseModel):
    id: str
    state: str
    answer: str | None
    error: str | None
    total_tokens: int
    cost_usd: float
    tool_calls: list[dict[str, Any]] = []


class CancelResult(BaseModel):
    id: str
    state: str


class ApproveBody(BaseModel):
    tool_name: str
    args: dict[str, Any]
    # Accepted for backward compatibility but ignored: the approver is
    # bound to the authenticated JWT principal, never a client-supplied
    # string (that was approver impersonation).
    approved_by: str | None = None


class ApproveResult(BaseModel):
    action_id: str
    nonce: str
    expires_at: str


# ---- Auth ----


def issue_token(principal_id: str, settings=None) -> str:
    settings = settings or get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": principal_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_expiry_seconds)).timestamp()),
        "principal_id": principal_id,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def require_principal(authorization: str | None = Header(None)) -> str:
    """JWT auth gate. Raises 401 on missing/invalid bearer."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer")
    token = authorization.split(" ", 1)[1].strip()
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"invalid token: {exc}") from exc
    pid = payload.get("principal_id") or payload.get("sub")
    if not pid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing principal_id claim")
    return pid


# ---- Endpoints ----


@app.post("/v1/runs", response_model=RunView, status_code=status.HTTP_201_CREATED)
def create_run(body: CreateRunBody, principal_id: str = Depends(require_principal)) -> RunView:
    if body.graph_version not in GRAPH_VERSION_TO_TOPOLOGY:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"unknown graph_version: {body.graph_version!r}. "
                f"Supported: {sorted(GRAPH_VERSION_TO_TOPOLOGY)}"
            ),
        )
    run_id = uuid.uuid4().hex[:32]
    code_sha = os.environ.get("AGENTOPS_CODE_SHA", "dev-sha")
    with session_scope() as s:
        run = Run(
            id=run_id,
            principal_id=principal_id,
            state=RunState.QUEUED.value,
            graph_version=body.graph_version,
            prompt_version=body.prompt_version,
            model_config=body.model_config,
            budget=body.budget,
            code_sha=code_sha,
            task=body.task,
        )
        s.add(run)
        s.flush()

    # Synchronous execution for MVP (Step 5 moves this to a job runner)
    _execute_run(run_id, corpus_dir_override=body.corpus_dir or None)
    return get_run(run_id, principal_id)


def _persist_tool_calls(session, run_id: str, tool_results: list[dict[str, Any]]) -> None:
    """One ToolCall row per executed planner_executor step.

    fixed/single_agent never produce tool_results, so this is a no-op for
    them -- their tool_calls count stays 0, by design. action_key reuses
    the existing state.make_action_key() dedup helper (run_id, tool_name,
    canonical-hash(args)), keyed on the REAL args the tool was invoked with
    (`entry["args"]` -- `{"query": task}` for search_docs, `{"doc_id": ...}`
    for read_document, `{}` otherwise), not a step-position placeholder --
    a retry that re-dispatches the same real call must land on the same
    action_key to satisfy the crash-recovery idempotency invariant
    documented in graph/state.py. args_canonical is stored as the dict
    itself (ToolCall.args_canonical is a JSON column, same shape as
    Action.args_canonical below -- not a double-encoded JSON string).
    """
    for entry in tool_results:
        tool_name = entry["tool_name"]
        args = entry.get("args") or {}
        action_key = make_action_key(run_id, tool_name, args)
        outcome_payload: dict[str, Any] = {"status": entry["outcome"]}
        if entry.get("error_kind"):
            outcome_payload["error_kind"] = entry["error_kind"]
        session.add(
            ToolCall(
                id=uuid.uuid4().hex[:32],
                run_id=run_id,
                tool_name=tool_name,
                # No approval gate applies to read-only retrieval tools
                # (search_docs/read_document/get_issue); only
                # create_ticket_draft/publish_ticket go through
                # POST /v1/actions. "auto" records that this call was
                # dispatched without a human-in-the-loop approval step.
                policy_decision="auto",
                action_key=action_key,
                args_canonical=args,
                outcome=outcome_payload,
                latency_ms=entry.get("latency_ms", 0),
            )
        )


def _execute_run(run_id: str, *, corpus_dir_override: str | None = None) -> None:
    settings = get_settings()
    adapter = make_adapter(settings)
    try:
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                return
            task = run.task
            run.state = RunState.RUNNING.value

        topology_name = GRAPH_VERSION_TO_TOPOLOGY.get(run.graph_version)
        if topology_name is None:  # pragma: no cover - guarded at create_run time
            with session_scope() as s:
                run = s.get(Run, run_id)
                if run is not None:
                    run.state = RunState.FAILED.value
                    run.error = f"unknown graph_version: {run.graph_version!r}"
            return

        # When wiki_dir is set, the agent reads from that directory
        # instead of the fixture docs. `corpus_dir_override` (passed by
        # create_run from the per-run `corpus_dir` body field) wins over
        # both env-derived fields. `wiki_mode` is True only when wiki_dir
        # was the resolver's source — a per-run override forces
        # wiki_mode=False so the operator gets the original
        # InMemoryDocumentClient (substring scan), not a silent
        # retrieval-algorithm swap.
        corpus_dir, wiki_mode = settings.resolved_corpus(override=corpus_dir_override)
        # execute graph (synchronous; bounded by each topology's own step budget)
        try:
            result = run_topology(
                topology_name, adapter, task, corpus_dir=corpus_dir, wiki_mode=wiki_mode
            )
        except Exception as exc:  # pragma: no cover - exercised via test_failure
            log.exception("graph execution failed")
            with session_scope() as s:
                run = s.get(Run, run_id)
                if run is not None:
                    run.state = RunState.FAILED.value
                    run.error = f"graph_error: {exc}"
            return
        usage = adapter.last_usage
        with session_scope() as s:
            run = s.get(Run, run_id)
            if run is None:
                return
            run.answer = result["answer"]
            state_val = result["state"]
            run.state = state_val.value if isinstance(state_val, RunState) else state_val
            if usage is not None:
                run.total_tokens = usage.total_tokens
                run.cost_usd = usage.cost_usd
            tool_results = result.get("tool_results", [])
            if tool_results:
                _persist_tool_calls(s, run_id, tool_results)
    finally:
        adapter.close()


def get_run(run_id: str, principal_id: str) -> RunView:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None or run.principal_id != principal_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        tool_calls = [
            {
                "id": tc.id,
                "tool_name": tc.tool_name,
                "policy_decision": tc.policy_decision,
                "action_key": tc.action_key,
                "outcome": tc.outcome,
                "latency_ms": tc.latency_ms,
            }
            for tc in run.tool_calls
        ]
        return RunView(
            id=run.id,
            state=run.state,
            answer=run.answer,
            error=run.error,
            total_tokens=run.total_tokens,
            cost_usd=run.cost_usd,
            tool_calls=tool_calls,
        )


@app.get("/v1/runs/{run_id}", response_model=RunView)
def get_run_endpoint(run_id: str, principal_id: str = Depends(require_principal)) -> RunView:
    return get_run(run_id, principal_id)


@app.post("/v1/runs/{run_id}/cancel", response_model=CancelResult)
def cancel_run(run_id: str, principal_id: str = Depends(require_principal)) -> CancelResult:
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None or run.principal_id != principal_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if is_terminal(RunState(run.state)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"cannot cancel run in terminal state {run.state!r}",
            )
        run.state = RunState.CANCELLED.value
    return CancelResult(id=run_id, state=RunState.CANCELLED.value)


# ---- Dev-mode auto-mint (Phase 9 UX fix) ----
#
# Without this, every request to the web UI has to carry a hand-pasted
# JWT — an awful UX for local dev. Streamlit already has the same
# capability gated by AGENTOPS_ALLOW_DEV_TOKEN=1; the web UI needs
# the equivalent.
#
# Gate: this endpoint is ONLY served when
#   provider == "local-fake"   (the offline fake provider)
# AND settings.allow_dev_token is True (operator opt-in via env).
# Production deployments with a real LLM and a real ID provider
# never expose this — the auth path is `require_principal`.


class DevTokenResponse(BaseModel):
    token: str
    principal_id: str
    expires_in: int


def _dev_token_allowed() -> bool:
    """Return True iff GET /v1/auth/dev-token should be served.

    Two independent gates:
      1. `allow_dev_token` -- the original opt-in. Without it, dev-mode
         auto-mint is off (the default).
      2. Either `provider == "local-fake"` (the offline fake) OR
         `dev_token_any_provider` (a deliberate second opt-in for local
         use with a real provider).

    The second gate (`dev_token_any_provider`) is deliberately separate
    from `provider` so a real provider never silently re-enables
    unauthenticated token-minting on its own: the operator has to set
    BOTH flags on purpose. Without that, the original
    provider==local-fake-only gate is unchanged -- a real deployment
    with provider=minimax gets a 403 even if AGENTOPS_ALLOW_DEV_TOKEN=1
    is set, which is the safety default.
    """
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001 - dev-only probe
        return False
    if not settings.allow_dev_token:
        return False
    return settings.provider == "local-fake" or settings.dev_token_any_provider


@app.get("/v1/auth/dev-token", response_model=DevTokenResponse)
def dev_token(principal_id: str = "dev-user") -> DevTokenResponse:
    """Mint a fresh JWT for `principal_id`. Dev-only.

    Disabled by default. Enable with `AGENTOPS_ALLOW_DEV_TOKEN=1` AND
    EITHER `AGENTOPS_PROVIDER=local-fake` (the dev default) OR
    `AGENTOPS_DEV_TOKEN_ANY_PROVIDER=1` (a deliberate second opt-in for
    a local/demo run against a real provider). A real deployment with
    `provider=minimax|openai` and only the first flag set
    still gets a 403 -- prevents accidentally shipping dev-mode auth to
    prod. (`provider=anthropic` raises `NotImplementedError` at
    adapter construction, so dev-token auth never even runs.)
    """
    if not _dev_token_allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "/v1/auth/dev-token is dev-only. Set AGENTOPS_ALLOW_DEV_TOKEN=1 "
                "and either provider=local-fake or AGENTOPS_DEV_TOKEN_ANY_PROVIDER=1."
            ),
        )
    settings = get_settings()
    pid = principal_id.strip() or settings.dev_principal_id
    tok = issue_token(pid, settings)
    return DevTokenResponse(
        token=tok,
        principal_id=pid,
        expires_in=settings.jwt_expiry_seconds,
    )


@app.get("/v1/auth/dev-mode", response_model=dict)
def dev_mode() -> dict:
    """Report whether dev-mode auto-mint is on, so the web UI can
    know to skip the manual Bearer field."""
    return {
        "enabled": _dev_token_allowed(),
        "provider": get_settings().provider,
        "default_principal": get_settings().dev_principal_id,
    }


@app.post("/v1/actions", response_model=ApproveResult, status_code=status.HTTP_201_CREATED)
def create_action(body: ApproveBody, run_id: str, principal_id: str = Depends(require_principal)) -> ApproveResult:
    """Create a pending action record (bound nonce + expiry)."""
    nonce = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    action_id = hashlib.sha256(f"{run_id}:{body.tool_name}:{nonce}".encode()).hexdigest()[:32]
    with session_scope() as s:
        run = s.get(Run, run_id)
        if run is None or run.principal_id != principal_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        action = Action(
            id=action_id,
            run_id=run_id,
            tool_name=body.tool_name,
            args_canonical=body.args,
            nonce=nonce,
            expires_at=expires_at,
            approved_by=principal_id,
        )
        s.add(action)
    return ApproveResult(action_id=action_id, nonce=nonce, expires_at=expires_at.isoformat())


# ---- debug endpoints ----


@app.get("/_debug/retrieve", response_model=dict)
def debug_retrieve(task: str) -> dict:
    settings = get_settings()
    if settings.provider != "local-fake" and not os.environ.get("AGENTOPS_ALLOW_DEBUG_METRICS"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "/_debug/retrieve is dev-only. Set provider=local-fake or "
                "AGENTOPS_ALLOW_DEBUG_METRICS=1 to enable on a real provider."
            ),
        )
    docs_dir = (Path(__file__).parent.parent.parent / "fixtures" / "docs")
    from ..mcp import InMemoryDocumentClient
    client = InMemoryDocumentClient(docs_dir=str(docs_dir))
    return client.search_docs(task, top_k=5)


@app.get("/_debug/metrics", response_model=dict)
def debug_metrics() -> dict:
    settings = get_settings()
    if settings.provider != "local-fake" and not os.environ.get("AGENTOPS_ALLOW_DEBUG_METRICS"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "/_debug/metrics is dev-only. Set provider=local-fake or "
                "AGENTOPS_ALLOW_DEBUG_METRICS=1 to enable on a real provider."
            ),
        )
    return {
        "test_count": dev_metrics.test_count(),
        "db_stats": dev_metrics.db_stats(),
        "screenshots": dev_metrics.screenshot_stats(),
        "line_diff_vs_main": dev_metrics.line_diff_vs_main(),
        "settings": {
            "provider": settings.provider,
            "model": settings.model,
        },
        "recent_cost_usd": dev_metrics.recent_cost_usd(),
        "caveats": {
            "cost_usd": (
                "Local-fake always returns 0.0 (fixture is free). For minimax/openai, "
                "cost is computed locally as prompt_tokens/1M * input_per_1m + "
                "completion_tokens/1M * output_per_1m; edit "
                "src/agentops_workbench/llm/pricing.py DEFAULT_PRICING or set "
                "AGENTOPS_PRICING_JSON env var to override. Unknown models return 0.0."
            ),
            "tool_calls": (
                "fixed-v1/single-agent-v1 stay at 0 by design; planner-executor-v1 "
                "executes a real TF-IDF search over the target repo's docs/README "
                "and surfaces the top-k matches as wiki evidence refs. "
                "With provider=local-fake (the CI default), the planner produces "
                "an 'I have no direct evidence' answer because the LLM has no real "
                "grounding context."
            ),
        },
    }


# ---- oss-helper (PR #36) web form ----
#
# The oss-helper form is intentionally a thin HTML page so the whole flow
# is reachable without Streamlit installed (per ADR-0008 exit criterion 2).
# All UI is self-contained CSS + a <form> POST; no JS, no client-side
# framework.

_OSS_HELPER_HTML = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>agentops-oss-helper</title>
<style>
:root {
  --bg: #fafafa;
  --fg: #1a1a1a;
  --muted: #6b7280;
  --border: #e5e7eb;
  --accent: #2563eb;
  --accent-bg: #eff6ff;
  --warn-bg: #fef3c7;
  --warn-fg: #92400e;
  --code-bg: #f3f4f6;
}
* { box-sizing: border-box }
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
       max-width: 960px; margin: 0 auto; padding: 32px 24px; color: var(--fg);
       background: var(--bg) }
header { margin-bottom: 24px }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em }
.tagline { color: var(--muted); font-size: 13px; margin: 0 }
.repo-badge { display: inline-flex; align-items: center; gap: 6px;
              background: var(--accent-bg); color: var(--accent);
              font-weight: 600; padding: 4px 10px; border-radius: 6px;
              font-family: ui-monospace, monospace; font-size: 13px;
              margin: 16px 0 0 }
.repo-badge .octicon { font-size: 12px }
.meta-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
            margin: 16px 0; font-size: 13px; color: var(--muted) }
.meta-chip { background: white; border: 1px solid var(--border); border-radius: 999px;
            padding: 3px 10px; font-size: 12px }
.meta-chip strong { color: var(--fg); font-weight: 600 }
.warn { background: var(--warn-bg); color: var(--warn-fg);
         padding: 10px 14px; border-radius: 6px; margin: 12px 0;
         font-size: 13px; border-left: 3px solid #f59e0b }
.section { background: white; border: 1px solid var(--border); border-radius: 10px;
          padding: 20px; margin: 16px 0 }
.section-title { font-size: 15px; font-weight: 700; color: var(--fg);
                margin: 0 0 14px; padding-bottom: 8px;
                border-bottom: 1px solid var(--border) }
.section-title .count { color: var(--muted); font-weight: 500; font-size: 13px;
                    margin-left: 6px; padding: 2px 8px;
                    border: 1px solid var(--border); border-radius: 999px;
                    vertical-align: 1px }
.ref-list { list-style: none; padding: 0; margin: 0 }
.ref-list li { padding: 10px 0; border-bottom: 1px solid var(--border);
              display: flex; gap: 10px; align-items: flex-start }
.ref-list li:last-child { border-bottom: 0 }
.ref-badge { flex-shrink: 0; font-family: ui-monospace, monospace; font-size: 12px;
             padding: 3px 8px; border-radius: 4px; font-weight: 600;
             text-decoration: none; min-width: 80px; text-align: center }
.ref-badge.wiki { background: var(--code-bg); color: var(--fg) }
.ref-content { flex: 1; min-width: 0 }
.ref-title { font-size: 14px; line-height: 1.35; margin: 0 0 4px; word-wrap: break-word }
.ref-score { font-size: 11px; color: var(--muted); font-family: ui-monospace, monospace }
.answer { white-space: pre-wrap; background: var(--code-bg); padding: 16px;
          border-radius: 8px; font: 14px/1.55 ui-monospace, monospace;
          border: 1px solid var(--border); margin: 0 }
.empty { color: var(--muted); padding: 16px;
          background: #f9fafb; border: 1px dashed var(--border);
          border-radius: 6px; text-align: center; font-size: 13px }
@media (max-width: 640px) {
  form { grid-template-columns: 1fr; }
  .ref-list li { flex-direction: column; gap: 6px }
  .ref-badge { align-self: flex-start }
}
</style></head><body>
<header>
<h1>agentops-oss-helper</h1>
<p class="tagline">Open Source Maintainer Helper Agent &mdash; paste a public
GitHub repo URL. The tool retrieves that repo's own docs/README (via
<code>git sparse-checkout</code>) and answers your question with citations.
No login, no write-back to the target repo.</p>
</header>
{repo_header}
<form method="post" action="/oss-helper">
  <input type="text" name="repo_url" required autofocus
         placeholder="https://github.com/<owner>/<repo>"
         value="{repo_url_value}">
  <input type="text" name="question"
         placeholder="optional free-text question">
  <button type="submit">Run</button>
</form>
</body></html>'''


@app.get("/oss-helper", response_class=HTMLResponse)
def oss_helper_form() -> HTMLResponse:
    """Minimal HTML form. No Streamlit dependency -- works in any browser.
    Proves the backend/frontend split: this whole flow is reachable
    without streamlit installed (per ADR-0008 exit criterion 2)."""
    return HTMLResponse(_format_oss_helper_html(""))


@app.post("/oss-helper", response_class=HTMLResponse)
async def oss_helper_run(
    repo_url: str = Form(...),
    question: str = Form(""),
) -> HTMLResponse:
    """Run the OSS Maintainer Helper flow and render an HTML report."""
    try:
        result = oss_helper.run_oss_helper(
            repo_url=repo_url,
            question=question.strip() or None,
        )
    except ValueError as exc:
        body = _format_oss_helper_html(repo_url)
        return HTMLResponse(
            body + f'<p class="warn">{exc}</p></body></html>'
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("oss-helper run failed")
        body = _format_oss_helper_html(repo_url)
        return HTMLResponse(
            body + f'<p class="warn">Internal error: {exc!r}</p></body></html>'
        )
    return HTMLResponse(_render_oss_helper_report(repo_url, result))


def _title(label: str, count: int) -> str:
    """Section title with a count badge."""
    return (
        f'<div class="section-title">{label}'
        f'<span class="count">{count}</span></div>'
    )


def _render_ref(ref: dict) -> str:
    """Render a single evidence ref link to its source."""
    rid = _html.escape(str(ref.get("ref_id", "")))
    title_text = _html.escape(str(ref.get("title", "")))
    score = ref.get("score", 0)
    return (
        f'<li>'
        f'<span class="ref-badge wiki">{rid}</span>'
        f'<div class="ref-content">'
        f'<div class="ref-title">{title_text}</div>'
        f'<div class="ref-score">score: {score:.2f}</div>'
        f'</div>'
        f'</li>'
    )


def _render_refs_section(title: str, refs: list[dict]) -> str:
    """Render one evidence section: header + list of clickable refs."""
    empty = (
        f'<div class="empty">No matching {title.lower()} found.</div>'
    ) if not refs else ''
    return (
        f'<section class="section">'
        f'{_title(title, len(refs))}'
        + empty
        + '<ul class="ref-list">' + "".join(_render_ref(r) for r in refs) + '</ul>'
        + '</section>'
    )


def _render_oss_helper_report(repo_url: str, result: oss_helper.TriageResult) -> str:
    """Render the TriageResult as HTML. Inline-only -- no client-side JS,
    no external assets, no Streamlit dependency."""
    warns = "".join(f'<p class="warn">{w}</p>' for w in result.warnings)
    meta_row = (
        f'<div class="meta-row">'
        f'<span class="meta-chip">docs: <strong>{len(result.wiki_refs)}</strong></span>'
        f'<span class="meta-chip">duration: <strong>{result.duration_ms}ms</strong></span>'
        f'<span class="meta-chip">provider: <strong>{_html.escape(_provider_name())}</strong></span>'
        f'</div>'
    )
    answer_section = (
        '<section class="section">'
        f'{_title("Answer", "")}'
        f'<div class="answer">{_html.escape(result.answer)}</div>'
        '</section>'
    )
    wiki_section = _render_refs_section(
        "Docs evidence (from the repo's own README/docs)",
        result.wiki_refs,
    )

    return (
        _format_oss_helper_html(repo_url, repo_header="")
        + warns
        + meta_row
        + answer_section
        + wiki_section
        + '</body></html>'
    )


def _provider_name() -> str:
    """Best-effort read of the current provider setting for the status row.
    Falls back to '?' if Settings isn't reachable (e.g. during a probe)."""
    try:
        return get_settings().provider
    except Exception:  # noqa: BLE001
        return "?"


def _format_oss_helper_html(repo_url: str, *, repo_header: str = "") -> str:
    """Substitute the per-request URL into the HTML template.

    Uses str.replace (not str.format) because the template's CSS contains
    brace literals that str.format would mis-parse as placeholders.
    """
    return _OSS_HELPER_HTML.replace(
        "{repo_url_value}", _html.escape(repo_url or "")
    ).replace(
        "{repo_header}", repo_header or ""
    )


# ---- Wiki corpus (Phase 9: browser-native directory picker) ----
#
# The browser File System Access API lets a reviewer pick a directory
# and read its .md files client-side. The browser POSTs each file's
# {path, content, mtime} here; the server writes them to a temp dir,
# builds a WikiRagAdapter (the same TF-IDF + cosine retrieval the
# planner_executor and single_agent topologies already use), and
# returns a corpus_id. corpus_id is process-local + LRU-capped;
# persistent storage is intentionally NOT supported (would mean
# keeping user files on server disk — a privacy regression).


class _WikiFileUpload(BaseModel):
    path: str
    content: str
    mtime: int = 0


class IndexFilesBody(BaseModel):
    files: list[_WikiFileUpload]
    # When the picked directory was an Obsidian vault, the browser
    # supplies the vault name so the server can construct
    # `obsidian://open?vault=<vault>&file=<path>` deep links for every
    # search hit. Optional: missing -> no deep links emitted (avoids
    # guessing a vault to open).
    vault_name: str | None = None
    # Retrieval algorithm for this corpus. None -> settings.wiki_default_retrieval.
    # "tfidf"/"bm25" are the original lexical scorers. "dense" (chunk-level
    # cosine over a small ONNX embedding model), "hybrid" (BM25+dense via
    # Reciprocal Rank Fusion) and "hybrid_rerank" (hybrid + a cross-encoder
    # rerank pass) are ADR-0010's additions -- CPU-only, no GPU required,
    # gated behind the optional `[dense]` install extra. Their `score` is on
    # a different scale than tfidf/bm25's (see ADR-0010 Consequences).
    # Pydantic rejects any other value with a 422 before this ever reaches
    # wiki_corpus.index_uploaded_files.
    retrieval: Literal["tfidf", "bm25", "dense", "hybrid", "hybrid_rerank"] | None = None


class IndexFilesResponse(BaseModel):
    corpus_id: str
    doc_count: int
    duration_ms: int
    # The mode that actually got resolved -- may differ from what the
    # client sent (None falls back to settings.wiki_default_retrieval).
    # The web UI attributes the live groundedness dashboard to this value
    # (ADR-0010 §4.5) since retrieval mode is fixed per corpus at index time.
    retrieval: str


class WikiHit(BaseModel):
    ref_id: str
    title: str
    score: float
    # AC3 trust fields.
    source_path: str
    evidence_span: str
    match_offsets: list[list[int]]
    coverage: float
    contributing_terms: list[str]
    mtime: int
    # Set only when the indexed directory was an Obsidian vault (browser
    # supplied vault_name on upload). The frontend uses this for a
    # one-click "open in vault" link on every hit.
    obsidian_uri: str | None = None


class WikiSearchResponse(BaseModel):
    query: str
    corpus_id: str
    top_k: int
    results: list[WikiHit]


class SentenceScore(BaseModel):
    sentence: str
    cited_refs: list[str]
    unresolved_refs: list[str]
    # Three published metrics per sentence.
    rouge_l_f1: float
    rouge_l_precision: float
    rouge_l_recall: float
    sentence_tokens: int
    evidence_tokens: int
    lcs_length: int


class QaResponse(BaseModel):
    query: str
    corpus_id: str
    answer: str
    # Echoed back so the client can rejoin the conversation on a
    # follow-up turn. Server mints one if `body.thread_id` is None.
    thread_id: str
    # Four answer-level published metrics.
    overall_rouge_l_f1: float
    citation_recall: float
    citation_precision: float
    # Faithfulness (Maynez et al., 2020) — mean sentence-level lexical-
    # entailment proxy across the answer, 0..1. Independent of citation
    # metrics: a sentence can cite [1] correctly AND still be unsupported
    # by evidence (paraphrased fabrication). Catches that case.
    faithfulness: float
    sentences: list[SentenceScore]
    hits: list[WikiHit]


class QaBody(BaseModel):
    corpus_id: str
    query: str
    top_k: int = 5
    # None -> start a new conversation (server mints a thread_id and
    # returns it in QaResponse). Pass the same value back on a follow-up
    # turn -- the LangGraph checkpointer in graph/wiki_chat.py restores
    # that thread's history automatically; the client never resends it.
    thread_id: str | None = None
    # UI provider picker (`GET /v1/wiki/providers` lists which of these
    # are actually usable, i.e. have a key configured server-side).
    # None -> settings.provider. The API key itself is NEVER accepted
    # here or anywhere else from the client -- it only ever comes from
    # server-side env vars via `Settings`.
    provider: Literal["local-fake", "minimax", "openai"] | None = None


class ProvidersResponse(BaseModel):
    available: list[str]
    default: str


class ProviderCheckResponse(BaseModel):
    provider: str
    # "ok" | "unconfigured" | one of LLMProviderError's kinds
    # (quota_exceeded / rate_limited / auth_failed / unavailable / unknown).
    status: str


@app.post("/v1/wiki/index-files", response_model=IndexFilesResponse)
def index_files(
    body: IndexFilesBody,
    principal_id: str = Depends(require_principal),
) -> IndexFilesResponse:
    """Accept the browser-uploaded {path, content, mtime} list, write
    them to a temp dir, and build a WikiRagAdapter. Returns corpus_id.

    Path validation: rejects absolute paths and any path containing
    `..` segments. The browser can technically read anywhere on the
    user's filesystem once the OS picker is approved; the server still
    refuses to write outside the temp dir even if the client is buggy
    or malicious.
    """
    import time as _time

    started = _time.monotonic()
    files_payload = [
        {"path": f.path, "content": f.content, "mtime": f.mtime}
        for f in body.files
    ]
    try:
        corpus_id, _work_dir, doc_count = wiki_corpus.index_uploaded_files(
            files_payload, vault_name=body.vault_name, retrieval=body.retrieval
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    duration_ms = int((_time.monotonic() - started) * 1000)
    resolved_retrieval = wiki_corpus.get_registry().get(corpus_id).adapter._retrieval
    log.info(
        "wiki index: principal=%s corpus_id=%s doc_count=%d files=%d duration_ms=%d "
        "retrieval=%s",
        principal_id, corpus_id, doc_count, len(files_payload), duration_ms,
        resolved_retrieval,
    )
    return IndexFilesResponse(
        corpus_id=corpus_id,
        doc_count=doc_count,
        duration_ms=duration_ms,
        retrieval=resolved_retrieval,
    )


@app.get("/v1/wiki/search", response_model=WikiSearchResponse)
def wiki_search_endpoint(
    corpus_id: str,
    q: str,
    top_k: int = 5,
    principal_id: str = Depends(require_principal),
) -> WikiSearchResponse:
    """TF-IDF search scoped to `corpus_id`. Returns AC3 trust fields
    on every hit. 404 if the corpus_id is unknown (evicted from LRU
    or never existed)."""
    effective_top_k = max(1, min(top_k, 20))
    try:
        hits, timing_ms = wiki_corpus.search_with_timing(
            corpus_id, q, top_k=effective_top_k
        )
    except wiki_corpus.UnknownCorpusError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"corpus_id not found: {exc.corpus_id}",
        ) from exc
    # Record one latency sample per call -- the dashboard's p50/p95
    # bars are built off this rolling window.
    search_metrics.record(
        StageSample(
            tokenize_ms=timing_ms.get("tokenize_ms", 0.0),
            score_ms=timing_ms.get("score_ms", 0.0),
            sort_and_return_ms=timing_ms.get("sort_and_return_ms", 0.0),
            total_ms=timing_ms.get("total_ms", 0.0),
        )
    )
    return WikiSearchResponse(
        query=q,
        corpus_id=corpus_id,
        top_k=effective_top_k,
        results=[WikiHit(**h.to_dict()) for h in hits],
    )


@app.get("/v1/wiki/metrics")
def wiki_metrics_endpoint(
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Aggregate observability for the wiki chat surface.

    Two views, both covering the trailing 200-call window:
      - `latency`: per-stage p50/p95 ms from /v1/wiki/search calls
        (tokenize / score / sort+return / total).
      - `groundedness`: per-call ROUGE-L F1, Citation Recall, Citation
        Precision averages from /v1/wiki/qa calls -- the
        "is the model hallucinating?" signal the operator watches.

    Same dev-only-ish posture as `/_debug/metrics`: the data here is
    operational, not customer-facing, but it is still behind the JWT
    gate so an unauthenticated probe can't enumerate timing."""
    return {
        "latency": search_metrics.stats(),
        "groundedness": groundedness_metrics.stats(),
    }


@app.post("/v1/wiki/metrics/reset")
def wiki_metrics_reset(
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Reset the in-memory rolling metrics for the wiki chat surface.

    Intended for the dev/demo path: when a new browser session starts,
    the dashboard would otherwise show stale server-uptime averages
    left over from the previous session. The UI's MetricsPanel calls
    this on mount so each fresh page-load starts at zero and builds up
    only from that session's chats.

    Production posture: gated on `AGENTOPS_ALLOW_WIKI_METRICS_RESET`.
    Both recorders are module-level singletons, so a successful call
    clears them for every active session sharing the server process
    -- any authenticated principal could otherwise wipe another
    session's observability (IDOR). Off-by-default keeps prod safe;
    flip it on for the dev/demo path where the MetricsPanel is useful.
    """
    if not get_settings().allow_wiki_metrics_reset:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="metrics reset is disabled (set AGENTOPS_ALLOW_WIKI_METRICS_RESET=true to enable)",
        )
    search_metrics.reset_for_tests()
    groundedness_metrics.reset_for_tests()
    return {
        "latency": search_metrics.stats(),
        "groundedness": groundedness_metrics.stats(),
    }


# LLMProviderError.kind -> (HTTP status, detail template). `quota_exceeded`
# and `rate_limited` get 429 (retryable by the caller, in principle -- a
# quota_exceeded retry will just fail again until the account is topped up,
# but 429 is still the closer HTTP semantic than a 5xx). `auth_failed` and
# `unavailable` are upstream/config problems the caller can't fix by
# retrying, so they get 502. Detail messages are operator-actionable, not
# just "LLM call failed" -- see the incident in llm/errors.py's docstring.
_LLM_ERROR_RESPONSES: dict[str, tuple[int, str]] = {
    "quota_exceeded": (
        status.HTTP_429_TOO_MANY_REQUESTS,
        "The {provider} account has run out of credits/quota. Add credits "
        "or switch providers (AGENTOPS_PROVIDER), then try again.",
    ),
    "rate_limited": (
        status.HTTP_429_TOO_MANY_REQUESTS,
        "The {provider} API is rate-limiting requests right now. Wait a "
        "moment and try again.",
    ),
    "auth_failed": (
        status.HTTP_502_BAD_GATEWAY,
        "The {provider} API rejected the configured API key. Check the "
        "AGENTOPS_{PROVIDER_UPPER}_API_KEY setting.",
    ),
    "unavailable": (
        status.HTTP_502_BAD_GATEWAY,
        "Could not reach the {provider} API (network error or timeout). "
        "Try again shortly.",
    ),
    "unknown": (
        status.HTTP_502_BAD_GATEWAY,
        "The {provider} API call failed: {message}",
    ),
}


def _llm_provider_http_exception(exc: LLMProviderError) -> HTTPException:
    status_code, template = _LLM_ERROR_RESPONSES.get(exc.kind, _LLM_ERROR_RESPONSES["unknown"])
    detail = template.format(
        provider=exc.provider, provider_upper=exc.provider.upper(),
        PROVIDER_UPPER=exc.provider.upper(), message=exc.message,
    )
    return HTTPException(status_code=status_code, detail=detail)


@app.get("/v1/wiki/providers", response_model=ProvidersResponse)
def wiki_providers(principal_id: str = Depends(require_principal)) -> ProvidersResponse:
    """List which LLM providers the UI's provider picker may offer.

    `local-fake` is always available (needs no key). `minimax`/`openai`
    are listed only when their API key is actually configured server-side
    -- offering a provider with no key would just fail on the first call
    with the `auth_failed` LLMProviderError. Never returns a key value.
    """
    settings = get_settings()
    available = ["local-fake"]
    if settings.minimax_api_key:
        available.append("minimax")
    if settings.openai_api_key:
        available.append("openai")
    return ProvidersResponse(available=available, default=settings.provider)


@app.post("/v1/wiki/providers/{provider}/check", response_model=ProviderCheckResponse)
def check_provider(
    provider: Literal["local-fake", "minimax", "openai"],
    principal_id: str = Depends(require_principal),
) -> ProviderCheckResponse:
    """Make one real, minimal test call to `provider` and report whether it
    actually works -- not just whether a key is present. Manually triggered
    from the UI (never automatic) because it spends a small amount of real
    money on a configured provider. Reports the SAME classified reason
    (`LLMProviderError.kind`) a real chat turn hitting this problem would
    get, so "quota exhausted" and "bad key" show up distinctly rather than
    both just being "broken".
    """
    settings = get_settings()
    if provider == "local-fake":
        return ProviderCheckResponse(provider=provider, status="ok")

    key = settings.minimax_api_key if provider == "minimax" else settings.openai_api_key
    if not key:
        return ProviderCheckResponse(provider=provider, status="unconfigured")

    try:
        adapter = make_adapter(settings, provider=provider)
    except ValueError:
        return ProviderCheckResponse(provider=provider, status="unconfigured")
    try:
        # max_tokens=16, not 1: a reasoning model (gpt-5.6-luna, o1, o3)
        # spends hidden reasoning tokens before any visible output, so a
        # 1-token budget fails with "Could not finish the message because
        # max_tokens ... was reached" even on a perfectly valid key --
        # confirmed via a real call, not assumed.
        adapter.chat([{"role": "user", "content": "ping"}], max_tokens=16)
    except LLMProviderError as exc:
        log.info("provider check: provider=%s status=%s", provider, exc.kind)
        return ProviderCheckResponse(provider=provider, status=exc.kind)
    except Exception:  # noqa: BLE001 - report as "unknown", never raise from a check
        log.exception("provider check: unexpected error provider=%s", provider)
        return ProviderCheckResponse(provider=provider, status="unknown")
    finally:
        adapter.close()
    return ProviderCheckResponse(provider=provider, status="ok")


@app.post("/v1/wiki/qa", response_model=QaResponse)
def wiki_qa(
    body: QaBody,
    principal_id: str = Depends(require_principal),
) -> QaResponse:
    """Multi-turn wiki chat. One turn = one retrieval + one LLM answer +
    per-sentence groundedness via the published metrics (ROUGE-L F1 —
    Lin, 2004; Citation Recall + Precision — Honovich et al., 2022).

    The LLM is prompted to ground every claim with a bracketed
    footnote number ([1], [2], ...) matching the evidence's own
    numbering. The server deterministically appends a References
    list mapping each number to its real source_path; a follow-up
    turn resumes the conversation by `thread_id`, transmitted only
    as a single opaque string -- the client never resends the
    transcript. The LangGraph checkpointer
    (`graph/wiki_chat.py::_WikiChatState.history`) restores history
    server-side, keyed by `thread_id` in `config.configurable`.
    """
    thread_id = body.thread_id or uuid.uuid4().hex
    settings = get_settings()
    try:
        adapter = make_adapter(settings, provider=body.provider)
    except ValueError as exc:
        # Missing API key for the requested provider (make_adapter raises
        # before any network call) -- a config problem the caller CAN fix
        # (pick a different provider, or an operator sets the key), unlike
        # the LLMProviderError cases below which are the provider's own
        # call failing after a key was already present.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # Capture per-stage search timing for the metrics dashboard.
    # The chat graph's internal `_retrieve_node` already times this
    # call, but doesn't surface the numbers to the API layer; the
    # cleanest thing for the operator's p50/p95 view is to re-time the
    # public call once more here, on the API thread. The cost is one
    # extra search per turn -- negligible vs the LLM latency.
    #
    # Skip the timing re-run entirely on the UnknownCorpusError path:
    # there's no benefit to recording a 404 search latency.
    search_timing_ms: dict[str, float] = {}
    try:
        _, search_timing_ms = wiki_corpus.search_with_timing(
            body.corpus_id, body.query, top_k=body.top_k
        )
    except wiki_corpus.UnknownCorpusError:
        # Re-raise below; just don't record latency on the error path.
        pass
    if search_timing_ms:
        search_metrics.record(
            StageSample(
                tokenize_ms=search_timing_ms.get("tokenize_ms", 0.0),
                score_ms=search_timing_ms.get("score_ms", 0.0),
                sort_and_return_ms=search_timing_ms.get("sort_and_return_ms", 0.0),
                total_ms=search_timing_ms.get("total_ms", 0.0),
            )
        )
    try:
        turn = run_wiki_chat(
            adapter, body.corpus_id, body.query, thread_id, top_k=body.top_k
        )
    except wiki_corpus.UnknownCorpusError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"corpus_id not found: {exc.corpus_id}",
        ) from exc
    except LLMProviderError as exc:
        log.warning(
            "wiki qa: LLM provider error principal=%s corpus_id=%s provider=%s kind=%s",
            principal_id, body.corpus_id, exc.provider, exc.kind,
        )
        raise _llm_provider_http_exception(exc) from exc
    finally:
        adapter.close()

    # Record one groundedness sample per call -- the dashboard's
    # "Accuracy / hallucination" panel aggregates these over the
    # trailing 200-call window so a reviewer can see whether the
    # model is drifting, not just what one turn did.
    groundedness_metrics.record(
        GroundednessSample(
            rouge_l_f1=turn.overall_rouge_l_f1,
            citation_recall=turn.citation_recall,
            citation_precision=turn.citation_precision,
            faithfulness=turn.faithfulness,
        )
    )
    log.info(
        "wiki qa: principal=%s corpus_id=%s thread_id=%s hits=%d sentences=%d "
        "rouge_l=%.3f cite_recall=%.3f cite_prec=%.3f faithful=%.3f",
        principal_id, body.corpus_id, thread_id, len(turn.hits), len(turn.sentences),
        turn.overall_rouge_l_f1, turn.citation_recall, turn.citation_precision,
        turn.faithfulness,
    )
    return QaResponse(
        query=body.query,
        corpus_id=body.corpus_id,
        thread_id=thread_id,
        answer=turn.answer,
        overall_rouge_l_f1=turn.overall_rouge_l_f1,
        citation_recall=turn.citation_recall,
        citation_precision=turn.citation_precision,
        faithfulness=turn.faithfulness,
        sentences=[SentenceScore(**s) for s in turn.sentences],
        hits=[WikiHit(**h) for h in turn.hits],
    )
