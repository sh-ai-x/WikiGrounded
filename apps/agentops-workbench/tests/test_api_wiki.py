"""TDD: /v1/wiki/{index-files,search,qa} endpoints.

AC1 — the browser POSTs uploaded files; the server returns corpus_id.
AC2 — search routes through the existing WikiRagAdapter (TF-IDF +
cosine), the same path used by planner_executor/single_agent.
AC3 — every search hit carries source_path, evidence_span, score,
coverage, contributing_terms, mtime. /qa additionally returns
per-sentence groundedness.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentops_workbench import wiki_corpus
from agentops_workbench.api.server import app, issue_token


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Drop any state from a previous test before this one runs."""
    wiki_corpus.reset_registry_for_tests()
    yield
    wiki_corpus.reset_registry_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_token('tester')}"}


# ---- AC1: directory picker → server → corpus_id ----


def test_index_files_requires_auth(client: TestClient) -> None:
    """Unauthenticated POSTs are refused at the JWT gate (401)."""
    r = client.post("/v1/wiki/index-files", json={"files": []})
    assert r.status_code == 401


def test_index_files_accepts_empty_list(client: TestClient, bearer: dict) -> None:
    r = client.post("/v1/wiki/index-files", json={"files": []}, headers=bearer)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "corpus_id" in body
    assert body["doc_count"] == 0


def test_index_files_creates_corpus_with_provided_files(
    client: TestClient, bearer: dict
) -> None:
    payload = {
        "files": [
            {
                "path": "notes/install.md",
                "content": (
                    "# Install\n"
                    "Install LangGraph with PostgreSQL checkpointing.\n"
                ),
                "mtime": 1_700_000_000_000,
            },
            {
                "path": "notes/auth.md",
                "content": (
                    "# Auth\n"
                    "JWT with HS256, generate a 48-byte secret.\n"
                ),
                "mtime": 1_700_000_001_000,
            },
        ]
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["doc_count"] == 2
    assert isinstance(body["corpus_id"], str) and len(body["corpus_id"]) >= 16


def test_index_files_accepts_bm25_retrieval_and_search_returns_hits(
    client: TestClient, bearer: dict
) -> None:
    payload = {
        "files": [
            {
                "path": "notes/install.md",
                "content": "Install LangGraph with PostgreSQL checkpointing.",
                "mtime": 0,
            },
        ],
        "retrieval": "bm25",
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 200, r.text
    corpus_id = r.json()["corpus_id"]

    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "postgresql checkpointing", "top_k": 5},
        headers=bearer,
    )
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits
    assert hits[0]["score"] > 0.0


def test_index_files_rejects_unknown_retrieval_mode(client: TestClient, bearer: dict) -> None:
    payload = {"files": [], "retrieval": "not-a-real-mode"}
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 422


def test_index_files_response_echoes_the_resolved_retrieval_mode(
    client: TestClient, bearer: dict
) -> None:
    """ADR-0010 §4.5: the dashboard needs to attribute its numbers to the
    mode that produced them, so the index-files response must say which
    mode actually got resolved (not just echo what the client asked for --
    a client that omits `retrieval` falls back to the settings default)."""
    payload = {
        "files": [{"path": "a.md", "content": "hello world", "mtime": 0}],
        "retrieval": "bm25",
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 200, r.text
    assert r.json()["retrieval"] == "bm25"


@pytest.mark.parametrize("mode", ["dense", "hybrid", "hybrid_rerank"])
def test_index_files_accepts_non_lexical_modes_with_an_injected_backend(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """These modes need a real embedder in production; here we monkeypatch
    the module-level default so the API contract (a 200 + working search)
    is exercised without a network call or a real model download."""
    from tests.adapters._fakes import FakeEmbeddingBackend, FakeRerankBackend

    monkeypatch.setattr(
        "agentops_workbench.adapters.wiki_rag._default_embedding_backend",
        lambda: FakeEmbeddingBackend(keyword_vectors={"postgresql": [1.0], "checkpointing": [1.0]}),
    )
    monkeypatch.setattr(
        "agentops_workbench.adapters.wiki_rag._default_rerank_backend",
        lambda: FakeRerankBackend(),
    )
    payload = {
        "files": [
            {
                "path": "notes/install.md",
                "content": "# Install\nInstall LangGraph with PostgreSQL checkpointing.\n",
                "mtime": 0,
            },
        ],
        "retrieval": mode,
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 200, r.text
    assert r.json()["retrieval"] == mode
    corpus_id = r.json()["corpus_id"]

    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "postgresql checkpointing", "top_k": 5},
        headers=bearer,
    )
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits
    assert hits[0]["source_path"] == "notes/install.md"


def test_index_files_rejects_unsafe_paths(client: TestClient, bearer: dict) -> None:
    payload = {
        "files": [
            {"path": "../escape.md", "content": "x", "mtime": 0},
        ]
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 400


def test_index_files_rejects_absolute_paths(client: TestClient, bearer: dict) -> None:
    payload = {
        "files": [
            {"path": "/etc/passwd", "content": "x", "mtime": 0},
        ]
    }
    r = client.post("/v1/wiki/index-files", json=payload, headers=bearer)
    assert r.status_code == 400


# ---- AC2: search routes through WikiRagAdapter ----


def test_search_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/wiki/search", params={"corpus_id": "x", "q": "anything"})
    assert r.status_code == 401


def test_search_returns_404_for_unknown_corpus(
    client: TestClient, bearer: dict
) -> None:
    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": "nonexistent", "q": "anything"},
        headers=bearer,
    )
    assert r.status_code == 404


def test_search_after_index_returns_results_with_provenance(
    client: TestClient, bearer: dict
) -> None:
    # Index two files via the upload endpoint.
    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {
                    "path": "guides/install.md",
                    "content": (
                        "# Install\n"
                        "Install LangGraph with PostgreSQL checkpointing.\n"
                        "Use pip install langgraph[postgres].\n"
                    ),
                    "mtime": 1_700_000_000_000,
                },
                {
                    "path": "guides/auth.md",
                    "content": (
                        "# Auth\n"
                        "JWT with HS256, generate a 48-byte secret.\n"
                    ),
                    "mtime": 1_700_000_001_000,
                },
            ]
        },
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]

    # Search and check the AC3 trust fields land in the response.
    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "postgres checkpointing"},
        headers=bearer,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"] == "postgres checkpointing"
    assert body["corpus_id"] == corpus_id
    assert body["results"], "expected at least one hit"
    hit = body["results"][0]
    # AC3 trust fields are all present.
    for field_name in (
        "ref_id",
        "score",
        "source_path",
        "evidence_span",
        "match_offsets",
        "coverage",
        "contributing_terms",
        "mtime",
    ):
        assert field_name in hit, f"missing AC3 field: {field_name}"
    assert hit["source_path"] == "guides/install.md"
    assert hit["mtime"] == 1_700_000_000_000
    assert 0.0 < hit["coverage"] <= 1.0


def test_search_filters_top_k(client: TestClient, bearer: dict) -> None:
    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {"path": "a.md", "content": "alpha beta gamma", "mtime": 0},
                {"path": "b.md", "content": "alpha delta epsilon", "mtime": 0},
                {"path": "c.md", "content": "alpha zeta eta", "mtime": 0},
            ]
        },
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]
    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "alpha", "top_k": 2},
        headers=bearer,
    )
    body = r.json()
    assert len(body["results"]) <= 2


# ---- AC3: /qa returns per-sentence groundedness ----


def test_qa_requires_auth(client: TestClient) -> None:
    r = client.post(
        "/v1/wiki/qa",
        json={"corpus_id": "x", "query": "anything"},
    )
    assert r.status_code == 401


def test_qa_returns_per_sentence_groundedness(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch the LLM adapter so we don't need a real provider.

    Verifies the API surfaces the three published metrics:
      - per-sentence ROUGE-L F1 (Lin, 2004)
      - answer-level overall_rouge_l_f1
      - answer-level citation_recall + citation_precision (Honovich, 2022)
    """
    # Index one doc whose evidence supports a fully-grounded claim.
    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {
                    "path": "ref.md",
                    "content": (
                        "PostgresSaver writes durable checkpoints to "
                        "a PostgreSQL table for long-running agents."
                    ),
                    "mtime": 0,
                }
            ]
        },
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]

    # Patch the QA adapter factory to a stub that returns a known answer.
    from agentops_workbench.api import server as server_mod

    # The corpus contains one file `ref.md` (flat file -> ref_id "ref").
    # Sentence 1 fully matches the evidence; sentence 2 is disjoint and
    # cites a fabricated ref so we can verify citation_precision < 1.
    class _StubAdapter:
        def chat(self, messages, **kwargs):
            class _Result:
                content = (
                    "PostgresSaver writes durable checkpoints. [1] "
                    "MongoDB clusters horizontally. [2]"
                )
            return _Result()

        last_usage = None

        def close(self) -> None:
            pass

    def _stub_factory(_settings, **_kwargs):
        return _StubAdapter()

    monkeypatch.setattr(server_mod, "make_adapter", _stub_factory)

    r = client.post(
        "/v1/wiki/qa",
        json={"corpus_id": corpus_id, "query": "what is PostgresSaver"},
        headers=bearer,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"]
    assert isinstance(body["sentences"], list)
    assert len(body["sentences"]) == 2
    # Per-sentence: all three published metrics surface in the response.
    for s in body["sentences"]:
        for field_name in (
            "rouge_l_f1",
            "rouge_l_precision",
            "rouge_l_recall",
            "cited_refs",
            "unresolved_refs",
        ):
            assert field_name in s, f"missing metric: {field_name}"
    # Sentence 1: sentence tokens are a strict subsequence of evidence,
    # so LCS == len(sentence) == 4. P = 4/4 = 1.0; R = 4/12 = 0.333;
    # F1 = 2*P*R/(P+R) = 0.5. This is the standard ROUGE-L behaviour for a
    # short sentence vs longer evidence — the harmonic mean penalises
    # the unbalanced coverage even when every sentence token is present.
    assert body["sentences"][0]["rouge_l_f1"] == pytest.approx(0.5, abs=0.01)
    assert body["sentences"][0]["rouge_l_precision"] == pytest.approx(1.0, abs=0.01)
    assert body["sentences"][0]["rouge_l_recall"] == pytest.approx(4 / 12, abs=0.01)
    # Sentence 2: disjoint from evidence -> ROUGE-L F1 = 0.0.
    assert body["sentences"][1]["rouge_l_f1"] == pytest.approx(0.0, abs=0.01)
    # Sentence 2 also has an unresolved citation -> reflected in field.
    # [2] is out of range (only one hit, footnote [1], was retrieved).
    assert "2" in body["sentences"][1]["unresolved_refs"]
    # Answer-level: three published metrics all surface.
    for field_name in ("overall_rouge_l_f1", "citation_recall", "citation_precision"):
        assert field_name in body, f"missing answer-level metric: {field_name}"
    # Macro-average ROUGE-L F1 across the two sentences = (0.5 + 0.0) / 2.
    assert body["overall_rouge_l_f1"] == pytest.approx(0.25, abs=0.01)
    # Citation Recall: 1 of 2 sentences has a resolved citation -> 0.5.
    assert body["citation_recall"] == pytest.approx(0.5, abs=0.01)
    # Citation Precision: 1 valid (ref) + 1 invalid (ref-bogus) out of 2 -> 0.5.
    assert body["citation_precision"] == pytest.approx(0.5, abs=0.01)


def test_qa_returns_404_for_unknown_corpus(client: TestClient, bearer: dict) -> None:
    r = client.post(
        "/v1/wiki/qa",
        json={"corpus_id": "nonexistent", "query": "anything"},
        headers=bearer,
    )
    assert r.status_code == 404


# ---- LLM provider error handling (quota/rate-limit/auth/network) ----


def _index_one_file(client: TestClient, bearer: dict) -> str:
    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {"path": "ref.md", "content": "PostgresSaver writes durable checkpoints.", "mtime": 0}
            ]
        },
        headers=bearer,
    )
    return r.json()["corpus_id"]


@pytest.mark.parametrize(
    ("kind", "expected_status", "expected_detail_substring"),
    [
        ("quota_exceeded", 429, "run out of credits"),
        ("rate_limited", 429, "rate-limiting"),
        ("auth_failed", 502, "rejected the configured API key"),
        ("unavailable", 502, "Could not reach"),
        ("unknown", 502, "API call failed"),
    ],
)
def test_qa_maps_llm_provider_errors_to_friendly_http_responses(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
    kind: str, expected_status: int, expected_detail_substring: str,
) -> None:
    """Regression: a MiniMax quota-exhaustion error used to propagate as an
    unhandled 500 with no actionable message (see llm/errors.py)."""
    from agentops_workbench.api import server as server_mod
    from agentops_workbench.llm.errors import LLMProviderError

    corpus_id = _index_one_file(client, bearer)

    class _FailingAdapter:
        last_usage = None

        def chat(self, messages, **kwargs):
            raise LLMProviderError(kind, "minimax", "simulated failure")

        def close(self) -> None:
            pass

    monkeypatch.setattr(server_mod, "make_adapter", lambda _s, **_kw: _FailingAdapter())

    r = client.post(
        "/v1/wiki/qa",
        json={"corpus_id": corpus_id, "query": "what does PostgresSaver do"},
        headers=bearer,
    )
    assert r.status_code == expected_status, r.text
    assert expected_detail_substring in r.json()["detail"]


# ---- LLM provider picker (UI provider selection, keys stay server-side) ----


def test_providers_endpoint_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/wiki/providers")
    assert r.status_code == 401


def test_providers_endpoint_always_lists_local_fake(client: TestClient, bearer: dict) -> None:
    r = client.get("/v1/wiki/providers", headers=bearer)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "local-fake" in body["available"]
    assert body["default"] == "local-fake"


def test_providers_endpoint_lists_openai_only_when_a_key_is_configured(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentops_workbench.settings as settings_mod

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-test")
    settings_mod._settings = None
    try:
        r = client.get("/v1/wiki/providers", headers=bearer)
        assert r.status_code == 200, r.text
        assert "openai" in r.json()["available"]
    finally:
        settings_mod._settings = None


def test_providers_endpoint_never_includes_key_values(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentops_workbench.settings as settings_mod

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-super-secret-value")
    settings_mod._settings = None
    try:
        r = client.get("/v1/wiki/providers", headers=bearer)
        assert "sk-super-secret-value" not in r.text
    finally:
        settings_mod._settings = None


def test_check_provider_requires_auth(client: TestClient) -> None:
    r = client.post("/v1/wiki/providers/openai/check")
    assert r.status_code == 401


def test_check_provider_local_fake_is_always_ok(client: TestClient, bearer: dict) -> None:
    r = client.post("/v1/wiki/providers/local-fake/check", headers=bearer)
    assert r.status_code == 200, r.text
    assert r.json() == {"provider": "local-fake", "status": "ok"}


def test_check_provider_reports_unconfigured_when_no_key_is_set(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This worktree's own .env may configure a real (possibly broken) key --
    # explicitly clear it so this test exercises the "no key at all" case
    # regardless of local dev setup.
    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "")
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None
    try:
        r = client.post("/v1/wiki/providers/openai/check", headers=bearer)
    finally:
        settings_mod._settings = None
    assert r.status_code == 200, r.text
    assert r.json() == {"provider": "openai", "status": "unconfigured"}


def test_check_provider_makes_a_real_call_and_reports_the_classified_failure(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured-but-broken key (e.g. quota exhausted) must report the
    SAME classified reason a real chat turn would hit -- not just "it has
    a key" (which was the bug this feature replaces)."""
    from agentops_workbench.api import server as server_mod
    from agentops_workbench.llm.errors import LLMProviderError

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-test-key")
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None

    class _FailingAdapter:
        last_usage = None

        def chat(self, messages, **kwargs):
            raise LLMProviderError("quota_exceeded", "openai", "no credits")

        def close(self) -> None:
            pass

    monkeypatch.setattr(server_mod, "make_adapter", lambda _s, **_kw: _FailingAdapter())
    try:
        r = client.post("/v1/wiki/providers/openai/check", headers=bearer)
    finally:
        settings_mod._settings = None
    assert r.status_code == 200, r.text
    assert r.json() == {"provider": "openai", "status": "quota_exceeded"}


def test_check_provider_reports_ok_on_a_successful_call(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentops_workbench.api import server as server_mod

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-test-key")
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None

    class _OkAdapter:
        last_usage = None

        def chat(self, messages, **kwargs):
            class _Result:
                content = "pong"
            return _Result()

        def close(self) -> None:
            pass

    monkeypatch.setattr(server_mod, "make_adapter", lambda _s, **_kw: _OkAdapter())
    try:
        r = client.post("/v1/wiki/providers/openai/check", headers=bearer)
    finally:
        settings_mod._settings = None
    assert r.status_code == 200, r.text
    assert r.json() == {"provider": "openai", "status": "ok"}


def test_check_provider_uses_a_max_tokens_budget_big_enough_for_reasoning_models(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `max_tokens=1` made the check fail against gpt-5.6-luna
    with 'Could not finish the message because max_tokens ... was reached'
    -- a reasoning model spends hidden reasoning tokens before any visible
    output, so a 1-token budget can never succeed. Caught via a real call,
    not assumed; confirmed max_tokens=16 works."""
    from agentops_workbench.api import server as server_mod

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-test-key")
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None

    captured: dict[str, object] = {}

    class _CapturingAdapter:
        last_usage = None

        def chat(self, messages, **kwargs):
            captured.update(kwargs)
            class _Result:
                content = "pong"
            return _Result()

        def close(self) -> None:
            pass

    monkeypatch.setattr(server_mod, "make_adapter", lambda _s, **_kw: _CapturingAdapter())
    try:
        r = client.post("/v1/wiki/providers/openai/check", headers=bearer)
    finally:
        settings_mod._settings = None
    assert r.status_code == 200, r.text
    assert captured.get("max_tokens", 0) >= 16
    assert r.json() == {"provider": "openai", "status": "ok"}


def test_check_provider_never_leaks_the_key_value(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agentops_workbench.settings as settings_mod

    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "sk-super-secret-marker")
    settings_mod._settings = None
    try:
        r = client.post("/v1/wiki/providers/openai/check", headers=bearer)
        assert "sk-super-secret-marker" not in r.text
    finally:
        settings_mod._settings = None


def test_qa_accepts_a_provider_override_and_passes_it_to_make_adapter(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentops_workbench.api import server as server_mod

    corpus_id = _index_one_file(client, bearer)
    captured: dict[str, object] = {}

    class _StubAdapter:
        last_usage = None

        def chat(self, messages, **kwargs):
            class _Result:
                content = "Answer. [1]"
            return _Result()

        def close(self) -> None:
            pass

    def _stub_factory(_settings, *, provider=None):
        captured["provider"] = provider
        return _StubAdapter()

    monkeypatch.setattr(server_mod, "make_adapter", _stub_factory)

    r = client.post(
        "/v1/wiki/qa",
        json={"corpus_id": corpus_id, "query": "what does PostgresSaver do", "provider": "openai"},
        headers=bearer,
    )
    assert r.status_code == 200, r.text
    assert captured["provider"] == "openai"


def test_qa_returns_a_clear_error_when_the_requested_providers_key_is_missing(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This worktree's own .env may configure a real (possibly broken) key --
    # explicitly clear it so this test exercises "no key at all", not
    # whatever happens to be configured locally, and never makes a real
    # network call.
    monkeypatch.setenv("AGENTOPS_OPENAI_API_KEY", "")
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None

    corpus_id = _index_one_file(client, bearer)
    try:
        r = client.post(
            "/v1/wiki/qa",
            json={"corpus_id": corpus_id, "query": "what does PostgresSaver do", "provider": "openai"},
            headers=bearer,
        )
    finally:
        settings_mod._settings = None
    assert r.status_code == 400, r.text
    assert "openai" in r.json()["detail"].lower()


# ---- Dev-mode auto-mint (Phase 9 UX) ----


@pytest.fixture
def dev_mode_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable AGENTOPS_ALLOW_DEV_TOKEN=1 and reset the settings cache."""
    monkeypatch.setenv("AGENTOPS_ALLOW_DEV_TOKEN", "1")
    monkeypatch.setenv("AGENTOPS_PROVIDER", "local-fake")
    import agentops_workbench.settings as _settings
    _settings._settings = None
    yield
    _settings._settings = None


@pytest.fixture
def dev_mode_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure AGENTOPS_ALLOW_DEV_TOKEN=0 and reset the settings cache."""
    monkeypatch.setenv("AGENTOPS_ALLOW_DEV_TOKEN", "0")
    monkeypatch.setenv("AGENTOPS_PROVIDER", "local-fake")
    import agentops_workbench.settings as _settings
    _settings._settings = None
    yield
    _settings._settings = None


def test_dev_mode_status_reports_enabled(
    client: TestClient, dev_mode_on: None
) -> None:
    r = client.get("/v1/auth/dev-mode")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["provider"] == "local-fake"


def test_dev_mode_status_reports_disabled_by_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With AGENTOPS_ALLOW_DEV_TOKEN unset (and dev_token_any_provider
    unset too), dev-mode is off and the endpoint reports it. Both must
    be unset because either alone is enough to enable auto-mint.

    Like the second-flag tests, the on-disk `.env` overrides monkey-
    patched env, so this test patches the singleton's settings directly
    to simulate the unset state -- otherwise the live `.env` (which
    deliberately has both flags set for the local demo) would mask
    what we're trying to verify."""
    import agentops_workbench.api.server as server_mod
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None
    s = settings_mod.get_settings()
    s.allow_dev_token = False  # type: ignore[misc]
    s.dev_token_any_provider = False  # type: ignore[misc]
    server_mod.search_metrics.reset_for_tests()
    try:
        r = client.get("/v1/auth/dev-mode")
        assert r.status_code == 200
        assert r.json()["enabled"] is False
    finally:
        settings_mod._settings = None


def test_dev_token_endpoint_returns_jwt_when_enabled(
    client: TestClient, dev_mode_on: None
) -> None:
    r = client.get("/v1/auth/dev-token", params={"principal_id": "reviewer"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 50
    assert body["principal_id"] == "reviewer"
    assert body["expires_in"] > 0
    # Token must actually authenticate. Use /v1/wiki/index-files with
    # an empty body — auth is the gate we want to verify, the 200 is
    # the success path (corpus_id for an empty corpus).
    r2 = client.post(
        "/v1/wiki/index-files",
        json={"files": []},
        headers={"Authorization": f"Bearer {body['token']}"},
    )
    assert r2.status_code == 200, r2.text


def test_dev_token_endpoint_refuses_when_disabled(
    client: TestClient, dev_mode_off: None
) -> None:
    r = client.get("/v1/auth/dev-token", params={"principal_id": "reviewer"})
    assert r.status_code == 403


def test_dev_token_uses_default_principal_when_blank(
    client: TestClient, dev_mode_on: None
) -> None:
    r = client.get("/v1/auth/dev-token", params={"principal_id": "  "})
    assert r.status_code == 200
    assert r.json()["principal_id"] == "dev-user"


# ---- Per-stage search timing + /v1/wiki/metrics aggregate endpoint ----


def test_search_records_per_stage_timing_into_metrics_aggregator(
    client: TestClient, bearer: dict
) -> None:
    """Each /v1/wiki/search call records one sample of per-stage
    latency into the rolling-window aggregator. After several calls the
    /v1/wiki/metrics endpoint exposes p50/p95 of those samples."""
    from agentops_workbench.api import server as server_mod

    server_mod.search_metrics.reset_for_tests()

    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {
                    "path": "notes/install.md",
                    "content": "Install LangGraph with PostgreSQL checkpointing.",
                    "mtime": 0,
                },
                {"path": "notes/auth.md", "content": "JWT HS256 with a 48-byte secret.", "mtime": 0},
            ]
        },
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]

    for _ in range(5):
        r = client.get(
            "/v1/wiki/search",
            params={"corpus_id": corpus_id, "q": "checkpointing"},
            headers=bearer,
        )
        assert r.status_code == 200

    metrics = client.get("/v1/wiki/metrics", headers=bearer).json()
    assert metrics["latency"]["total_ms"]["count"] == 5
    assert metrics["latency"]["total_ms"]["p50"] >= 0.0
    assert metrics["latency"]["total_ms"]["p95"] >= metrics["latency"]["total_ms"]["p50"], (
        "p95 must be >= p50 by definition"
    )
    for stage in ("tokenize_ms", "score_ms", "sort_and_return_ms", "total_ms"):
        assert stage in metrics["latency"]
        assert metrics["latency"][stage]["count"] == 5


def test_metrics_endpoint_reports_groundedness_aggregates_across_chat_calls(
    client: TestClient, bearer: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The metrics endpoint exposes the average ROUGE-L/citation scores
    across recent /v1/wiki/qa calls -- the hallucination/accuracy panel."""
    from agentops_workbench.api import server as server_mod
    from agentops_workbench.llm.adapter import ChatResult, LLMAdapter, Usage

    server_mod.search_metrics.reset_for_tests()
    server_mod.groundedness_metrics.reset_for_tests()

    class _Stub(LLMAdapter):
        provider = "stub"
        model = "stub-v1"

        def chat(self, messages, **kw):
            return ChatResult(
                content="Answer cites real evidence. [ref]",
                usage=Usage(provider="stub", model="stub-v1", prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
            )

    monkeypatch.setattr(server_mod, "make_adapter", lambda _s, **_kw: _Stub())

    r = client.post(
        "/v1/wiki/index-files",
        json={"files": [{"path": "ref.md", "content": "real evidence text", "mtime": 0}]},
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]

    for _ in range(3):
        client.post(
            "/v1/wiki/qa",
            json={"corpus_id": corpus_id, "query": "explain", "top_k": 5},
            headers=bearer,
        )

    metrics = client.get("/v1/wiki/metrics", headers=bearer).json()
    g = metrics["groundedness"]
    assert g["sample_count"] == 3
    assert 0.0 <= g["rouge_l_f1_avg"] <= 1.0
    assert 0.0 <= g["citation_recall_avg"] <= 1.0
    assert 0.0 <= g["citation_precision_avg"] <= 1.0


def test_metrics_endpoint_is_auth_gated(client: TestClient) -> None:
    r = client.get("/v1/wiki/metrics")
    assert r.status_code == 401


def test_metrics_reset_clears_both_recorders(client: TestClient, bearer: dict) -> None:
    """POST /v1/wiki/metrics/reset zeroes both the latency and
    groundedness rolling windows. This is what the web UI calls once
    on page mount so a fresh browser tab doesn't inherit whatever a
    long-running dev server accumulated from earlier sessions."""
    from agentops_workbench.api import server as server_mod
    from agentops_workbench.wiki_metrics import GroundednessSample, StageSample

    # These recorders are process-wide singletons shared across every
    # test in this session -- clear them first so an earlier test's
    # samples don't pollute the count assertions below.
    server_mod.search_metrics.reset_for_tests()
    server_mod.groundedness_metrics.reset_for_tests()
    server_mod.search_metrics.record(
        StageSample(tokenize_ms=1.0, score_ms=1.0, sort_and_return_ms=1.0, total_ms=3.0)
    )
    server_mod.groundedness_metrics.record(
        GroundednessSample(
            rouge_l_f1=0.5, citation_recall=0.5, citation_precision=0.5, faithfulness=0.5
        )
    )
    before = client.get("/v1/wiki/metrics", headers=bearer).json()
    assert before["latency"]["total_ms"]["count"] == 1
    assert before["groundedness"]["sample_count"] == 1

    r = client.post("/v1/wiki/metrics/reset", headers=bearer)
    assert r.status_code == 200
    after = r.json()
    assert after["latency"]["total_ms"]["count"] == 0
    assert after["groundedness"]["sample_count"] == 0
    assert after["groundedness"]["faithfulness_avg"] == 0.0

    # And the follow-up GET reflects the same cleared state.
    confirmed = client.get("/v1/wiki/metrics", headers=bearer).json()
    assert confirmed["latency"]["total_ms"]["count"] == 0
    assert confirmed["groundedness"]["sample_count"] == 0


def test_metrics_reset_is_auth_gated(client: TestClient) -> None:
    r = client.post("/v1/wiki/metrics/reset")
    assert r.status_code == 401


# ---- Obsidian deep links on hit titles ----


def test_search_hit_carries_obsidian_uri_when_index_dir_is_an_obsidian_vault(
    client: TestClient, bearer: dict
) -> None:
    """When the indexed directory's root contained a `.obsidian/`
    subdirectory at upload time, every hit must carry an `obsidian_uri`
    -- the constructed `obsidian://open?vault=<vault>&file=<path>`
    deep-link the frontend can open in the user's vault without
    hand-editing."""
    r = client.post(
        "/v1/wiki/index-files",
        json={
            "files": [
                {
                    "path": "wiki/langgraph/checkpointing.md",
                    "content": "LangGraph checkpointing via PostgresCheckpointer.",
                    "mtime": 0,
                }
            ],
            "vault_name": "MyVault",
        },
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]
    r = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "checkpointing"},
        headers=bearer,
    )
    body = r.json()
    assert body["results"], "expected at least one hit"
    assert "obsidian_uri" in body["results"][0]
    assert body["results"][0]["obsidian_uri"] == (
        "obsidian://open?vault=MyVault&file=wiki/langgraph/checkpointing.md"
    )


def test_search_hit_obsidian_uri_omitted_when_no_vault_name_supplied(
    client: TestClient, bearer: dict
) -> None:
    r = client.post(
        "/v1/wiki/index-files",
        json={"files": [{"path": "a.md", "content": "checkpointing", "mtime": 0}]},
        headers=bearer,
    )
    corpus_id = r.json()["corpus_id"]
    body = client.get(
        "/v1/wiki/search",
        params={"corpus_id": corpus_id, "q": "checkpointing"},
        headers=bearer,
    ).json()
    assert body["results"]
    assert body["results"][0].get("obsidian_uri") in (None, ""), (
        "no vault_name -> no deep link (avoids guessing a vault to open)"
    )


# ---- Dev-mode auto-mint on a real provider (the deliberate
# double-opt-in for a local demo with no real login system) ----


@pytest.fixture
def dev_mode_on_real_provider_without_second_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import agentops_workbench.settings as _settings
    _settings._settings = None
    monkeypatch.setenv("AGENTOPS_ALLOW_DEV_TOKEN", "1")
    monkeypatch.setenv("AGENTOPS_PROVIDER", "minimax")
    monkeypatch.delenv("AGENTOPS_DEV_TOKEN_ANY_PROVIDER", raising=False)
    _settings._settings = None
    yield
    _settings._settings = None


@pytest.fixture
def dev_mode_on_real_provider_with_second_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTOPS_ALLOW_DEV_TOKEN", "1")
    monkeypatch.setenv("AGENTOPS_PROVIDER", "minimax")
    monkeypatch.setenv("AGENTOPS_DEV_TOKEN_ANY_PROVIDER", "1")
    import agentops_workbench.settings as _settings
    _settings._settings = None
    yield
    _settings._settings = None


def test_dev_mode_stays_off_for_real_provider_without_the_second_flag(
    client: TestClient, dev_mode_on_real_provider_without_second_flag: None
) -> None:
    """Regression guard: setting a real provider must never, by itself,
    re-enable the unauthenticated dev-token HTTP endpoint -- that would
    silently reintroduce the exact prod-exposure risk the original
    provider==local-fake gate existed to prevent.

    Both monkeypatch and a `get_settings` patch are needed: pydantic-
    settings reads `.env` directly, so monkeypatching os.environ alone
    leaves the second flag in scope. The patch below overrides the
    singleton's return value to the no-second-flag case, which is the
    cleanest way to test the gate's logic in isolation from the
    on-disk `.env`."""
    import agentops_workbench.api.server as server_mod
    import agentops_workbench.settings as settings_mod
    settings_mod._settings = None
    # Re-read settings under the fixture's monkeypatched env: provider
    # and allow_dev_token are set, dev_token_any_provider is unset.
    s = settings_mod.get_settings()
    s.dev_token_any_provider = False  # type: ignore[misc]
    server_mod.search_metrics.reset_for_tests()
    try:
        r = client.get("/v1/auth/dev-mode")
        assert r.json()["enabled"] is False

        r = client.get("/v1/auth/dev-token")
        assert r.status_code == 403
    finally:
        settings_mod._settings = None


def test_dev_mode_enabled_for_real_provider_with_explicit_second_flag(
    client: TestClient, dev_mode_on_real_provider_with_second_flag: None
) -> None:
    """The deliberate double opt-in: both AGENTOPS_ALLOW_DEV_TOKEN=1 and
    AGENTOPS_DEV_TOKEN_ANY_PROVIDER=1 set -> auto-mint works with a real
    provider too, restoring the zero-manual-paste UX for local/demo use."""
    r = client.get("/v1/auth/dev-mode")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["provider"] == "minimax"

    r = client.get("/v1/auth/dev-token", params={"principal_id": "reviewer"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["token"], str) and len(body["token"]) > 50
    assert body["principal_id"] == "reviewer"
