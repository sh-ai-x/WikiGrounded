"""Runtime settings — driven by env vars; .env is gitignored."""
from __future__ import annotations

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_JWT_SECRETS = {"", "dev-only-please-rotate"}
_ALLOWED_JWT_ALGORITHMS = {"HS256", "HS384", "HS512"}
_ALLOWED_REASONING_EFFORTS = {"", "none", "low", "medium", "high", "xhigh", "max"}


class Settings(BaseSettings):
    """Read from environment (and .env if present). All env vars are prefixed AGENTOPS_."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Provider
    provider: str = "local-fake"
    model: str = "MiniMax-M3"  # valid on api.minimax.io/v1
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.chat/v1"
    openai_api_key: str = ""

    # Reasoning effort for GPT-5-family / o1 / o3 "reasoning" models on
    # OpenAI's Chat Completions API (`OpenAICompatAdapter`) -- controls how
    # many hidden reasoning tokens the model spends before answering.
    # Empty string = unset -> the param is omitted from the API call
    # entirely (required for non-reasoning models like gpt-4o-mini, which
    # reject an unrecognized `reasoning_effort` field). Ignored by
    # `MinimaxAdapter` and `LocalFakeAdapter`.
    reasoning_effort: str = ""

    # App
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    jwt_secret: str = "dev-only-please-rotate"
    jwt_algorithm: str = "HS256"
    jwt_expiry_seconds: int = 3600

    # DB
    database_url: str = "sqlite:///./agentops.db"

    # Document corpus (MCP document server + lexical retrieval).
    # Set wiki_dir to point the agent at any directory of *.md files
    # (e.g. an exported personal wiki); falls back to docs_dir when empty.
    # Uses WikiRagAdapter (TF-IDF over *.md) for the planner topology and
    # the same lexical scan as docs_dir for the fixed topology.
    #
    # Multi-tenant caveat (Major 6 in PR #39 review, marked PLAUSIBLE):
    # wiki_dir is read from process-global settings, NOT per principal_id.
    # In a multi-tenant deployment every authenticated principal reads the
    # same wiki tree. Either scope by principal_id (deferred — needs an
    # auth-aware corpus resolution path) or document this caveat to the
    # operator. This docstring is the documentation half; scoping is a
    # separate ADR-level decision.
    docs_dir: str = "fixtures/docs"
    wiki_dir: str = ""

    # Default retrieval algorithm for newly indexed wiki corpora
    # ("tfidf" or "bm25" -- Okapi BM25, k1=1.5 b=0.75). Per-request
    # override via IndexFilesBody.retrieval; this is just the fallback
    # when the client doesn't specify one.
    wiki_default_retrieval: str = "tfidf"

    # Trace export (OTel spans per run)
    runs_dir: str = "./runs"

    # Auth: principal for local dev
    dev_principal_id: str = "dev-user"

    # Dev-mode auto-mint: when True AND provider=local-fake, the API
    # exposes GET /v1/auth/dev-token that mints a fresh JWT on demand
    # so the web UI (and Streamlit) can authenticate without a
    # hand-pasted bearer. Production deployments leave this False
    # (default) and route JWTs through an ID provider (Auth0/Cognito/etc).
    allow_dev_token: bool = False

    # Dev/demo path: when True, exposes POST /v1/wiki/metrics/reset, which
    # wipes the process-wide wiki search/groundedness rolling windows
    # shared across every authenticated session. The MetricsPanel UI
    # calls it on mount so a fresh browser tab doesn't inherit samples
    # from earlier sessions, but the same call is also an authenticated
    # IDOR (any JWT-gated principal can zero every other session's
    # observability) -- defaulting to False keeps prod deployments
    # safe even if someone POSTs at the endpoint. Off-by-default mirrors
    # `allow_dev_token` so neither dev convenience opens by accident.
    allow_wiki_metrics_reset: bool = False

    # Deliberate second opt-in: lets auto-mint (allow_dev_token) also
    # cover a real provider (minimax/openai), for someone
    # running this locally as a demo with a real API key but without a
    # real login system. Requires BOTH flags explicitly set -- setting
    # a real provider must never, by itself, re-enable an
    # unauthenticated token-minting HTTP endpoint. Without this second
    # flag, the original provider==local-fake-only gate is unchanged.
    dev_token_any_provider: bool = False

    def resolved_corpus(self, override: str | None = None) -> tuple[str, bool]:
        """Resolve the (corpus_dir, wiki_mode) tuple for a single run.

        `override` is the per-run `corpus_dir` field on `CreateRunBody`;
        when set, it takes precedence over both env-derived fields.
        `wiki_mode` is True only when the resolved corpus was explicitly
        marked as a wiki directory (i.e. `wiki_dir` was set AND no per-run
        override replaced it). The planner/single_agent topologies use
        WikiRagAdapter only when wiki_mode is True; the fixed topology
        uses `_retrieve_docs` regardless.
        """
        if override:
            return (override, False)
        if self.wiki_dir:
            return (self.wiki_dir, True)
        return (self.docs_dir, False)

    @model_validator(mode="after")
    def _guard_jwt_algorithm(self) -> Settings:
        """Reject alg=none / unknown JWT algorithms (token-forgery guard)."""
        if self.jwt_algorithm not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"AGENTOPS_JWT_ALGORITHM must be one of {sorted(_ALLOWED_JWT_ALGORITHMS)}; "
                f"got {self.jwt_algorithm!r}."
            )
        return self

    @model_validator(mode="after")
    def _guard_wiki_default_retrieval(self) -> Settings:
        """Reject an unknown AGENTOPS_WIKI_DEFAULT_RETRIEVAL at startup
        rather than at the first /v1/wiki/index-files call -- mirrors
        ADR-0007 §4's "Unknown names fail at startup" and the
        `_guard_jwt_algorithm` idiom above. Imported inside the validator
        body (not at module scope) to avoid a `settings -> adapters.wiki_rag
        -> mcp` import cycle."""
        from .adapters.wiki_rag import _VALID_RETRIEVAL_MODES

        if self.wiki_default_retrieval not in _VALID_RETRIEVAL_MODES:
            raise ValueError(
                f"AGENTOPS_WIKI_DEFAULT_RETRIEVAL must be one of "
                f"{sorted(_VALID_RETRIEVAL_MODES)}; got "
                f"{self.wiki_default_retrieval!r}."
            )
        return self

    @model_validator(mode="after")
    def _guard_reasoning_effort(self) -> Settings:
        """Reject an unknown AGENTOPS_REASONING_EFFORT at startup rather
        than at the first reasoning-model API call."""
        if self.reasoning_effort not in _ALLOWED_REASONING_EFFORTS:
            raise ValueError(
                f"AGENTOPS_REASONING_EFFORT must be one of "
                f"{sorted(_ALLOWED_REASONING_EFFORTS)}; got "
                f"{self.reasoning_effort!r}."
            )
        return self

    def has_insecure_jwt_secret(self) -> bool:
        """True when the JWT secret is the dev default, empty, or too short.

        The API server refuses to start in this state unless
        provider == "local-fake" (the offline CI / unit-test path, ADR-0003
        — no network, no real principals). See api.server:_lifespan.
        """
        return self.jwt_secret in _INSECURE_JWT_SECRETS or len(self.jwt_secret) < 32


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


# Single source of truth for the default corpus directory. Imported
# wherever the literal would otherwise be duplicated; renaming the
# default now requires a single edit. Mirrors `Settings.docs_dir`'s
# default value, intentionally module-level so non-Settings callers
# (graph/topology.py etc.) don't need a Settings instance to get it.
DEFAULT_CORPUS_DIR = "fixtures/docs"
