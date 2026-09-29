"""Wiki chat graph -- checkpointed multi-turn wiki Q&A with footnote citations.

Why a graph here, specifically: a single retrieve -> LLM-call ->
respond turn has no branching, so wrapping it in LangGraph alone would
be ceremony with no functional benefit. What a plain function
genuinely can't give you is durable state across separate HTTP
requests without hand-rolling a store. That's exactly what multi-turn
chat needs: the client sends only `{corpus_id, query, thread_id}` --
not the whole growing transcript -- and the checkpointer restores
`history` for that `thread_id` automatically.

A checkpointed graph serializes every state field on each step (via
`MemorySaver`'s msgpack encoder), unlike the no-checkpointer graphs in
`single_agent.py`/`planner_executor.py` where state never leaves the
process. That rules out putting non-serializable runtime objects
(the live `LLMAdapter`, the `Tracer`) directly in state -- they go
through LangGraph's `config["configurable"]` instead, which is
passed to every node for a given `.invoke()` call but is never part
of the persisted checkpoint.

Per-node OTel spans attach at retrieve/answer boundaries. The
groundedness scorer is the same one used in the per-turn
`QaResponse`, so the metric the operator sees on the chat transcript
is the metric that lands in `groundedness_metrics` for the dashboard's
hallucination panel.

Citations are numbered, not raw ref_ids. The LLM cites `[1]`, `[2]`,
... matching the evidence's own numbering. The server deterministically
appends a References list mapping each number to its real source_path
(never let the LLM generate the mapping -- it could lie). The history
key keeps only the raw answer (no References block), so a follow-up
turn isn't bloated by the previous turn's footer.
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from .. import groundedness, wiki_corpus
from ..llm.adapter import LLMAdapter
from ..llm.errors import LLMProviderError
from ..observability.otel import Tracer

_QA_PROMPT = (
    "You are an OSS-maintainer assistant. Answer the question using "
    "ONLY the evidence below. Cite every claim with a bracketed "
    "footnote number matching the evidence's own numbering, e.g. [1]. "
    "Use only the numbers shown before each evidence block -- do not "
    "invent numbers or cite source paths directly. If the evidence "
    "does not support a claim, refuse explicitly rather than guessing. "
    "Answer in 2-3 paragraphs.\n\n"
    "Question: {query}\n\n## Evidence\n\n{evidence_blob}\n\n## Answer\n"
)

# Single source of truth for the References footer separator. Both the
# build site (`_answer_node`, where the footer is appended) and the split
# site (`run_wiki_chat`, where `cited_refs` is parsed back out) reference
# this constant. A future maintainer who changes only one site silently
# corrupts the other -- see review finding F1 on PR #51.
_REFERENCES_FOOTER_MARKER = "\n\n---\nReferences:"

_NO_EVIDENCE_ANSWER = (
    "I could not find relevant evidence in the picked wiki directory "
    "for that question."
)


def footnote_evidence_blob(
    hits: list[dict[str, Any]], evidence_map: dict[str, str]
) -> str:
    """Build the "## Evidence" section with numbered footnote labels.

    The numbering is by retrieval order: hit[0] -> [1], hit[1] -> [2], ...
    so the LLM-cited [1] / [2] / [3] line up with the rendered
    References block below the answer.

    Missing entries in `evidence_map` (defensive -- shouldn't happen
    but might during registry eviction mid-request) are rendered with
    an empty body rather than omitted, so the numbering stays
    consistent with the LLM's references."""
    if not hits:
        return ""
    return "\n\n--\n\n".join(
        f"[{i}] (source: {h['source_path']}, score: {h['score']:.3f})\n{evidence_map.get(h['ref_id'], '')}"
        for i, h in enumerate(hits, start=1)
    )


def make_references_block(hits: list[dict[str, Any]]) -> str:
    """Build the deterministic "References" footer mapping each
    footnote number back to its real source_path. One line per hit,
    never LLM-generated -- the mapping could lie if it were."""
    if not hits:
        return ""
    return "\n".join(f"[{i}] {h['source_path']}" for i, h in enumerate(hits, start=1))


def extract_cited_refs_from_answer(answer: str) -> list[str]:
    """Parse `[1] [2] [3]` footnote markers out of the LLM's response.
    Returns the ordered, de-duplicated list -- order matters because
    the LLM may cite [2] before [1] in prose, and the scorer should
    still respect that for `cited_refs` reporting."""
    import re
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"\[(\d+)\]", answer):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


@dataclass(frozen=True)
class ChatTurn:
    """One completed turn, returned to the API layer."""

    answer: str
    hits: list[dict[str, Any]]
    sentences: list[dict[str, Any]]
    overall_rouge_l_f1: float
    citation_recall: float
    citation_precision: float
    # Faithfulness (Maynez et al., 2020) -- mean sentence-level
    # lexical-entailment proxy, 0..1. Catches unsupported claims
    # even when the LLM cites correctly.
    faithfulness: float
    cited_refs: list[str] = field(default_factory=list)
    groundedness: dict[str, Any] = field(default_factory=dict)


class _WikiChatState(TypedDict, total=False):
    corpus_id: str
    query: str
    top_k: int
    hits: list[dict[str, Any]]
    history: Annotated[list[dict[str, str]], operator.add]
    answer: str
    sentences: list[dict[str, Any]]
    overall_rouge_l_f1: float
    citation_recall: float
    citation_precision: float
    faithfulness: float


def _retrieve_node(state: _WikiChatState, config: RunnableConfig) -> dict[str, Any]:
    tracer: Tracer | None = config.get("configurable", {}).get("tracer")
    span = tracer.start(
        "wiki.search",
        attributes={"corpus_id": state["corpus_id"], "query": state["query"], "top_k": state["top_k"]},
    ) if tracer else None
    try:
        hits, timing_ms = wiki_corpus.search_with_timing(
            state["corpus_id"], state["query"], top_k=state["top_k"]
        )
    except Exception:
        if tracer and span:
            tracer.end(span, status="error")
        raise
    if tracer and span:
        tracer.end(span, extra={"hit_count": len(hits)})
    return {"hits": [h.to_dict() for h in hits]}


def _answer_node(state: _WikiChatState, config: RunnableConfig) -> dict[str, Any]:
    hits = state.get("hits", [])
    query = state["query"]
    prior_history = state.get("history", [])

    if not hits:
        return {
            "answer": _NO_EVIDENCE_ANSWER,
            "sentences": [],
            "overall_rouge_l_f1": 0.0,
            "citation_recall": 0.0,
            "citation_precision": 0.0,
            "faithfulness": 0.0,
            "history": [
                {"role": "user", "content": query},
                {"role": "assistant", "content": _NO_EVIDENCE_ANSWER},
            ],
        }

    entry = wiki_corpus.get_registry().get(state["corpus_id"])
    evidence_map: dict[str, str] = {}
    for h in hits:
        try:
            evidence_map[h["ref_id"]] = entry.adapter.read_evidence(h["ref_id"], limit=2000)
        except Exception:
            evidence_map[h["ref_id"]] = ""

    evidence_blob = footnote_evidence_blob(hits, evidence_map)
    prompt_messages = list(prior_history) + [
        {"role": "user", "content": _QA_PROMPT.format(query=query, evidence_blob=evidence_blob)}
    ]

    configurable = config.get("configurable", {})
    adapter: LLMAdapter = configurable["adapter"]
    tracer: Tracer | None = configurable.get("tracer")
    span = tracer.start(
        "wiki.qa.llm_chat", attributes={"corpus_id": state["corpus_id"], "hit_count": len(hits)}
    ) if tracer else None
    try:
        chat = adapter.chat(prompt_messages, max_tokens=2048)
        raw_answer = (chat.content or "").strip()
    except Exception as exc:
        # `LLMAdapter.chat()` implementations classify their own provider's
        # exceptions into `LLMProviderError` (quota_exceeded / rate_limited
        # / auth_failed / unavailable / unknown) before raising -- tag the
        # span with that classification so a trace tells an operator WHY a
        # turn failed, not just that it did. Re-raised unchanged; the API
        # layer (`server.py::wiki_qa`) turns it into a proper HTTP status.
        if tracer and span:
            kind = exc.kind if isinstance(exc, LLMProviderError) else "unknown"
            tracer.end(span, status="error", extra={"error_kind": kind})
        raise
    if tracer and span:
        tracer.end(span)

    # Score on the raw answer (before adding the deterministic
    # References footer), so per-sentence citation counts reflect
    # only the LLM's own choices.
    score_input = {str(i): evidence_map[h["ref_id"]] for i, h in enumerate(hits, start=1)}
    scores = groundedness.groundedness_for_answer(raw_answer, score_input)
    overall_rouge_l = groundedness.answer_overall_rouge_l(scores)
    cit_recall = groundedness.answer_citation_recall(scores)
    cit_precision = groundedness.answer_citation_precision(scores)
    # Faithfulness (Maynez et al., 2020) is computed over the same
    # scored sentences + evidence_map. A separate concern from
    # citation metrics -- catches unsupported claims regardless of
    # whether the LLM cited them.
    faithful = groundedness.answer_faithfulness(scores, score_input)

    # Append the deterministic References footer to the DISPLAYED
    # answer only. Never to `history` (a follow-up turn shouldn't
    # re-pay the previous turn's footer tokens -- see module
    # docstring) and never to `raw_answer` itself (already scored
    # above; the footer is not part of the LLM's claims). The footer
    # maps each [N] the LLM cited back to a real source_path -- never
    # LLM-generated, so it can't lie about provenance.
    references_block = make_references_block(hits)
    display_answer = (
        f"{raw_answer}{_REFERENCES_FOOTER_MARKER}\n{references_block}"
        if references_block
        else raw_answer
    )

    new_turns = [
        {"role": "user", "content": query},
        {"role": "assistant", "content": raw_answer},
    ]
    return {
        "answer": display_answer,
        "sentences": [s.to_dict() for s in scores],
        "overall_rouge_l_f1": overall_rouge_l,
        "citation_recall": cit_recall,
        "citation_precision": cit_precision,
        "faithfulness": faithful,
        "history": new_turns,
    }


def _build_graph():
    graph = StateGraph(_WikiChatState)
    graph.add_node("retrieve", _retrieve_node)
    graph.add_node("answer", _answer_node)
    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "answer")
    graph.add_edge("answer", END)
    return graph.compile(checkpointer=MemorySaver())


wiki_chat_graph = _build_graph()


def run_wiki_chat(
    adapter: LLMAdapter,
    corpus_id: str,
    query: str,
    thread_id: str,
    *,
    top_k: int = 5,
    tracer: Tracer | None = None,
) -> ChatTurn:
    """Run one turn of the wiki chat, resuming `thread_id`'s history.

    `thread_id` is opaque to the caller -- mint one with `uuid4().hex`
    for a new conversation and pass the same value back for follow-ups.
    The checkpointer (keyed by `thread_id` in `config.configurable`)
    restores `history` automatically; the caller never resends it.
    `adapter`/`tracer` are runtime-only, passed via `configurable`
    (not state) precisely so they never hit the checkpointer's
    serializer.
    """
    result = wiki_chat_graph.invoke(
        {"corpus_id": corpus_id, "query": query, "top_k": top_k},
        config={"configurable": {"thread_id": thread_id, "adapter": adapter, "tracer": tracer}},
    )
    # `result["answer"]` includes the deterministic References footer
    # -- parse cited_refs off the raw answer, before the footer, so we
    # only see the references the LLM actually invoked (not every hit
    # we listed). The node's `state["answer"]` (raw, no footer) is
    # not directly accessible here, so split on the footer separator.
    full_answer = result["answer"]
    raw = full_answer.split(_REFERENCES_FOOTER_MARKER, 1)[0]
    cited_refs = extract_cited_refs_from_answer(raw)
    return ChatTurn(
        answer=full_answer,
        hits=result.get("hits", []),
        sentences=result.get("sentences", []),
        overall_rouge_l_f1=result.get("overall_rouge_l_f1", 0.0),
        citation_recall=result.get("citation_recall", 0.0),
        citation_precision=result.get("citation_precision", 0.0),
        faithfulness=result.get("faithfulness", 0.0),
        cited_refs=cited_refs,
    )
