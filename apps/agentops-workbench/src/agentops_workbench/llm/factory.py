"""Factory — picks an adapter by provider name. The graph uses only this."""
from __future__ import annotations

from ..settings import Settings
from .adapter import LLMAdapter
from .local_fake import LocalFakeAdapter
from .minimax import MinimaxAdapter

# Sensible default model per provider, used ONLY when the caller overrides
# `settings.provider` to a different provider than the one actually
# configured (the UI provider picker) -- `settings.model` is tuned for
# whichever provider `settings.provider` names, and sending that string to
# a different provider's API would just 404/error on an unknown model.
# When the override matches `settings.provider`, the operator's configured
# `settings.model` is used unchanged, exactly as before this override
# existed.
_DEFAULT_MODEL_BY_PROVIDER: dict[str, str] = {
    "minimax": "MiniMax-M3",
    # gpt-5.6-luna: OpenAI's current cost/performance-optimized tier
    # ($0.20/$1.20 per 1M input/output tokens) -- see
    # docs/adr/ (reasoning_effort is configured separately via
    # AGENTOPS_REASONING_EFFORT, not baked into the model name).
    "openai": "gpt-5.6-luna",
}


def make_adapter(settings: Settings, *, provider: str | None = None) -> LLMAdapter:
    """Build an LLMAdapter for `provider`, falling back to `settings.provider`.

    `provider` is the UI provider-picker override (`QaBody.provider`); it
    never changes which API key is used -- keys stay exactly where
    `Settings` already reads them from (env / `.env`), never from the
    client.
    """
    configured = settings.provider.lower()
    p = (provider or settings.provider).lower()
    model = settings.model if p == configured else _DEFAULT_MODEL_BY_PROVIDER.get(p, settings.model)

    if p == "local-fake":
        return LocalFakeAdapter()
    if p == "minimax":
        return MinimaxAdapter(
            api_key=settings.minimax_api_key,
            base_url=settings.minimax_base_url,
            model=model,
        )
    if p == "openai":
        # Imported lazily; this branch ships in step 4 experiments
        from .openai_compat import OpenAICompatAdapter

        return OpenAICompatAdapter(
            api_key=settings.openai_api_key,
            base_url="https://api.openai.com/v1",
            model=model,
            provider_label="openai",
            reasoning_effort=settings.reasoning_effort,
        )
    if p == "anthropic":
        # Anthropic's API is not OpenAI-compatible out of the box; shipping
        # a stub adapter against api.anthropic.com/v1 would 4xx on the first
        # call. Provider=anthropic is intentionally not implemented yet;
        # raise loudly rather than fail silently.
        raise NotImplementedError(
            "provider=anthropic is not implemented yet (Anthropic's API is "
            "not OpenAI-compatible; need a separate adapter)"
        )
    raise ValueError(f"unknown provider: {p!r}")
