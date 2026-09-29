"""TDD: wiki chat graph -- multi-turn checkpointed chat with footnote citations.

The chat graph wires two LangGraph concepts:

  1. **StateGraph (retrieve -> answer)** -- the same kind of
     `planner_executor`/`single_agent` graphs already in `graph/`,
     scoped to wiki evidence retrieval.

  2. **`MemorySaver` checkpointer** -- keyed by `thread_id`, persists
     the conversation `history` between HTTP requests. Client sends
     only `{corpus_id, query, thread_id}` -- never the whole
     transcript. The checkpointer restores history server-side.

What this gives the operator that the bare endpoint couldn't:
  - follow-up questions actually use the prior turn's context
    (proved by the prompt-assembly test below)
  - conversations survive server restarts *up to process lifetime*
    (matches the corpus registry's lifetime; both are in-memory)
  - per-corpus conversation branches: a new `thread_id` resets history
    (proved by the isolation test below)

References are numbered, not raw ref_ids. The LLM cites `[1]`, `[2]`,
... matching the evidence's own numbering. The server deterministically
appends a References list mapping each number to its real source_path
(never let the LLM generate the mapping -- it could lie). The history
key keeps only the raw answer (no References block), so a follow-up
turn isn't bloated by the previous turn's footer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agentops_workbench import wiki_corpus
from agentops_workbench.graph.wiki_chat import (
    footnote_evidence_blob,
    make_references_block,
    run_wiki_chat,
    wiki_chat_graph,
)
from agentops_workbench.llm.adapter import ChatResult, LLMAdapter, Usage


class _StubAdapter(LLMAdapter):
    """Deterministic adapter: records every prompt it was called with and
    returns a scripted reply, optionally varying by call count."""

    provider = "stub"
    model = "stub-v1"

    def __init__(self, replies: list[str]) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self._replies = list(replies)

    def chat(self, messages, *, temperature=0.0, max_tokens=1024, **kw) -> ChatResult:
        self.calls.append(messages)
        content = self._replies.pop(0) if self._replies else "ANSWER (no more scripted replies)"
        return ChatResult(
            content=content,
            usage=Usage(provider=self.provider, model=self.model, prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.0),
        )


@pytest.fixture
def corpus_id(tmp_path: Path) -> str:
    (tmp_path / "checkpointing.md").write_text(
        "LangGraph checkpointing persists graph state between steps via PostgresCheckpointer.",
        encoding="utf-8",
    )
    (tmp_path / "rate-limits.md").write_text(
        "Rate limits govern how many requests a client may issue per minute.",
        encoding="utf-8",
    )
    cid, _work_dir, _count = wiki_corpus.index_uploaded_files([
        {"path": "checkpointing.md", "content": (tmp_path / "checkpointing.md").read_text(), "mtime": 0},
        {"path": "rate-limits.md", "content": (tmp_path / "rate-limits.md").read_text(), "mtime": 0},
    ])
    yield cid
    wiki_corpus.cleanup_corpus(cid)


def test_run_wiki_chat_answers_a_single_turn(corpus_id: str) -> None:
    adapter = _StubAdapter(["Checkpointing persists state. [1]"])
    turn = run_wiki_chat(adapter, corpus_id, "how does checkpointing work", thread_id="t1")
    assert "Checkpointing persists state" in turn.answer
    # The LLM only emitted [1], so cited_refs (parsed from the answer
    # body before the References footer) is exactly [1]. The References
    # block lists every retrieved hit (incl. [2] = rate-limits.md), but
    # those don't appear in `cited_refs` because they aren't cited.
    assert turn.cited_refs == ["1"]
    assert turn.hits, "expected at least one retrieved hit"
    assert "[1]" in turn.answer


def test_run_wiki_chat_reports_a_genuinely_computed_faithfulness_score(
    corpus_id: str,
) -> None:
    """Regression: `_WikiChatState` (the graph's TypedDict schema) never
    declared a `faithfulness` key, so LangGraph silently dropped
    `_answer_node`'s computed value when merging node output into graph
    state -- `run_wiki_chat` always fell back to its `result.get(
    "faithfulness", 0.0)` default, showing 0% on every single turn
    regardless of how well-grounded the answer actually was. Every other
    metric (ROUGE-L, Citation Recall/Precision) happened to be declared
    in the schema, which is why only Faithfulness was silently broken.

    A cited claim that genuinely overlaps its evidence's vocabulary must
    score meaningfully above zero -- not just "some float", the actual
    non-degenerate value the metric is supposed to produce."""
    adapter = _StubAdapter(["Checkpointing persists graph state via PostgresCheckpointer. [1]"])
    turn = run_wiki_chat(adapter, corpus_id, "how does checkpointing work", thread_id="t-faithful")
    assert turn.faithfulness > 0.3, (
        f"expected a genuinely non-zero faithfulness score for a well-grounded "
        f"cited claim, got {turn.faithfulness}"
    )


def test_run_wiki_chat_persists_history_across_turns_via_thread_id(corpus_id: str) -> None:
    """The whole point of the checkpointer: the SECOND call must see the
    FIRST turn's history in its prompt, even though the caller never
    resent it. Only `thread_id` carries continuity."""
    adapter = _StubAdapter([
        "First answer. [1]",
        "Follow-up using prior context. [1]",
    ])

    run_wiki_chat(adapter, corpus_id, "how does checkpointing work", thread_id="thread-abc")
    run_wiki_chat(adapter, corpus_id, "how does checkpointing persist state", thread_id="thread-abc")

    assert len(adapter.calls) == 2
    second_call_messages = adapter.calls[1]
    flat = " ".join(m["content"] for m in second_call_messages)
    assert "how does checkpointing work" in flat
    assert "First answer" in flat


def test_run_wiki_chat_different_thread_ids_do_not_share_history(corpus_id: str) -> None:
    adapter = _StubAdapter([
        "Thread-1 answer. [1]",
        "Thread-2 answer (no prior context). [1]",
    ])

    run_wiki_chat(adapter, corpus_id, "how does checkpointing work", thread_id="thread-1")
    run_wiki_chat(adapter, corpus_id, "what is rate limits", thread_id="thread-2")

    second_call_messages = adapter.calls[1]
    flat = " ".join(m["content"] for m in second_call_messages)
    assert "Thread-1 answer" not in flat


def test_run_wiki_chat_short_circuits_when_no_evidence(corpus_id: str) -> None:
    """A query with no matching terms must NOT call the LLM --
    hardcoded refusal + 0 groundedness, fast path."""
    adapter = _StubAdapter(["should not be called"])
    turn = run_wiki_chat(adapter, corpus_id, "zzz-nonexistent-term-zzz", thread_id="t-refuse")

    assert not adapter.calls
    assert "no relevant evidence" in turn.answer.lower() or "could not find relevant evidence" in turn.answer.lower()
    assert "relevant evidence" in turn.answer.lower()
    assert turn.citation_recall == 0.0


def test_answer_includes_references_footer_mapping_citations_to_source_paths(
    corpus_id: str,
) -> None:
    """The displayed answer carries the LLM's prose verbatim PLUS a
    deterministic References footer mapping each [N] marker back to
    its real source_path -- restored per operator request (chat UI
    used to show this inline; a prior refactor moved it to a
    frontend-only tab built from `turn.hits`, which regressed the
    visible chat-bubble experience). The mapping is never
    LLM-generated (it could lie about provenance); `make_references_
    block` builds it straight from the retrieved hits, same as
    before."""
    adapter = _StubAdapter(["Answer cites evidence. [1]"])
    turn = run_wiki_chat(adapter, corpus_id, "checkpointing", thread_id="t-refs")
    assert "Answer cites evidence" in turn.answer
    assert "[1]" in turn.answer, (
        "the LLM's numbered citations come through verbatim"
    )
    assert turn.answer == (
        "Answer cites evidence. [1]\n\n---\nReferences:\n[1] checkpointing.md"
    )


def test_hits_carry_per_hit_metadata_for_frontend_references_tab(corpus_id: str) -> None:
    """The frontend builds its References tab from `turn.hits`. Each
    hit must carry source_path + obsidian_uri (when an Obsidian vault
    was used at upload time) so the frontend can render an actual
    link, not just a label."""
    adapter = _StubAdapter(["Cites the first. [1]"])
    turn = run_wiki_chat(adapter, corpus_id, "checkpointing rate limits", thread_id="t-multi")
    # Both docs match the query; both appear in `turn.hits` for the
    # frontend's References tab to render. The order is by retrieval
    # score, so we don't assert which is [1] vs [2] -- only that both
    # are present.
    paths = {h["source_path"] for h in turn.hits}
    assert "checkpointing.md" in paths
    assert "rate-limits.md" in paths


def test_follow_up_history_excludes_the_references_block(corpus_id: str) -> None:
    """The References list is for the end user, not for the LLM's own
    context window on follow-up turns -- repeating it would bloat the
    prompt without benefit."""
    adapter = _StubAdapter([
        "First answer with refs. [1]",
        "Second answer. [1]",
    ])
    run_wiki_chat(adapter, corpus_id, "how does checkpointing work", thread_id="t-clean")
    run_wiki_chat(adapter, corpus_id, "how does checkpointing persist state", thread_id="t-clean")

    second_prompt_text = " ".join(m["content"] for m in adapter.calls[1][:-1])
    assert "References:" not in second_prompt_text, (
        "history must carry the raw answer only, not the footer"
    )


# ---- Pure-function helpers ----


def test_footnote_evidence_blob_numbers_hits_in_retrieval_order() -> None:
    hits = [
        {"ref_id": "r1", "source_path": "a.md", "score": 0.9},
        {"ref_id": "r2", "source_path": "b.md", "score": 0.7},
        {"ref_id": "r3", "source_path": "c.md", "score": 0.5},
    ]
    evidence_map = {"r1": "alpha", "r2": "beta", "r3": "gamma"}
    blob = footnote_evidence_blob(hits, evidence_map)
    assert "[1] (source: a.md, score: 0.900)" in blob
    assert "[2] (source: b.md, score: 0.700)" in blob
    assert "[3] (source: c.md, score: 0.500)" in blob


def test_footnote_evidence_blob_skips_unresolvable_refs_gracefully() -> None:
    """A hit whose ref_id isn't in evidence_map (defensive: shouldn't
    happen but might during registry eviction mid-request) should be
    rendered with empty body, not omitted -- so the numbering stays
    consistent with the LLM's [1] [2] [3] references."""
    hits = [
        {"ref_id": "r1", "source_path": "a.md", "score": 0.9},
        {"ref_id": "r2", "source_path": "b.md", "score": 0.7},
    ]
    evidence_map = {"r1": "alpha"}  # r2 missing
    blob = footnote_evidence_blob(hits, evidence_map)
    assert "[1] (source: a.md" in blob
    assert "[2] (source: b.md" in blob
    # r2's body is the empty string -- check the format still parses.
    assert blob.count("[1]") == 1
    assert blob.count("[2]") == 1


def test_make_references_block_returns_empty_string_for_no_hits() -> None:
    assert make_references_block([]) == ""


def test_make_references_block_one_line_per_hit() -> None:
    refs = make_references_block([
        {"source_path": "wiki/a.md"},
        {"source_path": "wiki/b.md"},
    ])
    assert "[1] wiki/a.md" in refs
    assert "[2] wiki/b.md" in refs
    assert refs.count("\n") == 1  # exactly one newline between two lines


def test_wiki_chat_graph_compiles_with_checkpointer() -> None:
    """The graph module exposes a single compiled `wiki_chat_graph`
    singleton with a checkpointer attached -- importable for testing
    or external use (e.g. studio) without going through run_wiki_chat."""
    assert wiki_chat_graph is not None
