# ADR-0003: Provider abstraction (`LLMAdapter`)

## Status

Accepted (2026-09-08).

## Decision

- Single `LLMAdapter` interface returning normalized usage
  (`provider, model, prompt_tokens, completion_tokens, total_tokens, cost_usd`).
- Config-driven: `provider ∈ {openai, minimax, local-fake}`,
  `model=<id>`.
- CI / unit tests: `provider=local-fake` (deterministic, no API calls).
- Live experiments: `provider=minimax` (default).
- Other providers (`openai`) per-experiment via env.
- `anthropic` is intentionally not supported: the underlying API is not
  OpenAI-compatible, so a placeholder adapter would 4xx on first chat
  call. Constructing the adapter raises `NotImplementedError`
  (`src/agentops_workbench/llm/factory.py`).

## Consequences

- No provider-specific code paths in the agent graph; the adapter is
  the only place that names a provider.
- `langchain-minimax` does NOT yet exist as a published PyPI package
  (langchain-ai/langchain#36291). Use `ChatOpenAI(base_url=..., api_key=...)`
  labelled `minimax` in our adapter until upstream lands.
- A working `MINIMAX_API_KEY` is sourced from the sibling dev-harness-kit
  repo at `/Users/sanghee/dev/dev-harness-kit/.env`. Operators copy
  that value into this repo's `.env` (gitignored). The populated `.env`
  is NEVER committed.
- No GPU inference in this project; all model calls are HTTPS to a
  hosted LLM. Provider billing and rate limits are the only cost axes.
