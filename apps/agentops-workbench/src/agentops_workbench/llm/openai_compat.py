"""Generic OpenAI-compatible adapter — used for `provider=openai`.

The factory raises `NotImplementedError` for `provider=anthropic` because
Anthropic's API is not OpenAI-compatible out of the box; this module
intentionally does not contain a placeholder branch for it.
"""
from __future__ import annotations

from typing import Any

from .adapter import ChatResult, LLMAdapter, Usage
from .errors import classify_llm_error
from .pricing import cost_usd

# GPT-5-family / o1 / o3 "reasoning" models on OpenAI's Chat Completions API:
# `max_completion_tokens` replaces `max_tokens`, an optional `reasoning_effort`
# field controls how many hidden reasoning tokens the model spends before
# answering, and `temperature` must be omitted entirely -- a real call to
# gpt-5.6-luna returned "Unsupported value: 'temperature' does not support
# 0.0 with this model. Only the default (1) value is supported." Every
# caller in this codebase (graph/wiki_chat.py included) calls `.chat()`
# without an explicit temperature, i.e. this method's own default (0.0) --
# exactly the value a reasoning model rejects. An older chat model
# (gpt-4o-mini, MiniMax-M3) rejects an unrecognized `reasoning_effort`
# field outright, so that one must never be sent to one either.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3")


def _is_reasoning_model(model: str) -> bool:
    return model.lower().startswith(_REASONING_MODEL_PREFIXES)


class OpenAICompatAdapter(LLMAdapter):
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        provider_label: str,
        reasoning_effort: str = "",
    ) -> None:
        if not api_key:
            raise ValueError(f"API key required for provider={provider_label}")
        self.provider = provider_label
        self.model = model
        self._reasoning_effort = reasoning_effort
        from openai import OpenAI  # type: ignore[import-not-found]

        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kw: Any,
    ) -> ChatResult:
        create_kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if _is_reasoning_model(self.model):
            create_kwargs["max_completion_tokens"] = max_tokens
            if self._reasoning_effort:
                create_kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            create_kwargs["temperature"] = temperature
            create_kwargs["max_tokens"] = max_tokens
        create_kwargs.update(kw)
        try:
            resp = self._client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            raise classify_llm_error(exc, provider=self.provider) from exc
        content = (resp.choices[0].message.content or "").strip()
        u = resp.usage
        usage = Usage(
            provider=self.provider,
            model=self.model,
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            total_tokens=getattr(u, "total_tokens", 0) or 0,
            cost_usd=cost_usd(
                getattr(self, "model", "unknown"),
                getattr(u, "prompt_tokens", 0) or 0,
                getattr(u, "completion_tokens", 0) or 0,
            ),
        )
        self._last_usage = usage
        return ChatResult(content=content, usage=usage, raw=resp)
