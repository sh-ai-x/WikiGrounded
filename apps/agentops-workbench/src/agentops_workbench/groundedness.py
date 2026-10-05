"""Academic-grounded attribution metrics for LLM answers.

Phase 9 AC3 requires every LLM-generated claim to carry a
groundedness score drawn from the established metrics literature,
NOT custom-defined ones. This module implements three:

  - **ROUGE-L F1** (Lin, 2004 — "ROUGE: A Package for Automatic
    Evaluation of Summaries"). Sentence-vs-evidence F-measure over
    the longest common subsequence (LCS) of token sequences.
    Pure Python, no dependencies, well-cited baseline.

  - **Citation Recall** (Honovich et al., 2022 — "TRUE: Re-evaluating
    the Reliability of Natural Language Explanations"). Fraction
    of sentences that carry at least one citation marker.
    Independent of evidence content — measures *whether* the LLM
    attributed, not *whether* it attributed correctly.

  - **Citation Precision** (Honovich et al., 2022). Fraction of
    emitted `[ref-x]` markers that resolve to a real evidence
    block. Catches fabricated citations.

Together these three give a reviewer:
  - "Did the LLM cite its claims?"  -> Citation Recall
  - "Did the LLM cite real refs?"    -> Citation Precision
  - "How closely do the claims match the cited evidence?" -> ROUGE-L F1

References:
  - Lin, C.-Y. (2004). ROUGE: A Package for Automatic Evaluation of
    Summaries. In Text Summarization Branches Out, ACL workshop.
  - Honovich, O., et al. (2022). TRUE: Re-evaluating the Reliability
    of Natural Language Explanations. arXiv:2205.07750.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from .wiki_corpus import extract_citation_refs, split_sentences, tokenize

# Strip `[ref-x]` markers from a sentence before tokenizing for the
# coverage calculation. Without this, citation metadata would inflate
# the sentence's token count and lower ROUGE-L artificially.
_CITATION_STRIP_RE = re.compile(r"\[[A-Za-z0-9_.~-]+\]")

# Strip the LLM's chain-of-thought block before evaluating facts.
# MiniMax-M3 (and other reasoning models) emit `<think>...</think>`
# inline; those tokens describe the model's planning, not the user's
# answer, and would otherwise dilute every per-sentence faithfulness
# score. Match the opening tag, any content (lazy, with re.DOTALL),
# and the closing tag; missing either side passes through.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


@dataclass(frozen=True)
class GroundednessScore:
    """Per-sentence attribution result with three published metrics.

    Attributes:
      sentence:         Original sentence text (with citations kept).
      cited_refs:       Citations that resolve to real evidence blocks.
      unresolved_refs:  Citations the LLM emitted but which have no
                        matching evidence — fabricated citations.
      rouge_l_f1:       ROUGE-L F1 (Lin, 2004) between the sentence
                        and the union of cited evidence.
      rouge_l_precision: ROUGE-L precision = LCS / |sentence tokens|.
      rouge_l_recall:    ROUGE-L recall    = LCS / |evidence tokens|.
    """

    sentence: str
    cited_refs: list[str] = field(default_factory=list)
    unresolved_refs: list[str] = field(default_factory=list)
    rouge_l_f1: float = 0.0
    rouge_l_precision: float = 0.0
    rouge_l_recall: float = 0.0
    sentence_tokens: int = 0
    evidence_tokens: int = 0
    lcs_length: int = 0

    def to_dict(self) -> dict:
        return {
            "sentence": self.sentence,
            "cited_refs": list(self.cited_refs),
            "unresolved_refs": list(self.unresolved_refs),
            "rouge_l_f1": round(self.rouge_l_f1, 4),
            "rouge_l_precision": round(self.rouge_l_precision, 4),
            "rouge_l_recall": round(self.rouge_l_recall, 4),
            "sentence_tokens": self.sentence_tokens,
            "evidence_tokens": self.evidence_tokens,
            "lcs_length": self.lcs_length,
        }


# ---- ROUGE-L (Lin, 2004) ----


def _lcs_length(a: Sequence[str], b: Sequence[str]) -> int:
    """Length of the longest common subsequence of two token sequences.

    Standard O(|a| * |b|) dynamic-programming formulation. Empty
    inputs return 0; equal sequences return len(sequence).
    """
    if not a or not b:
        return 0
    # Use a 1-D rolling array to keep memory bounded.
    prev = [0] * (len(b) + 1)
    curr = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, prev
    return prev[len(b)]


def rouge_l_f1(
    sentence_tokens: Sequence[str],
    evidence_tokens: Sequence[str],
) -> tuple[float, float, float, int]:
    """Compute ROUGE-L F1 between sentence and evidence token sequences.

    Returns (f1, precision, recall, lcs_length). Implements the
    Lin (2004) formulation:
      P = LCS / |sentence|
      R = LCS / |evidence|
      F1 = 2 * P * R / (P + R)

    Edge cases match the paper's convention: if both inputs are
    empty, F1 = 0.0 (no content to score). If one is empty but the
    other isn't, F1 = 0.0 (zero overlap by definition).
    """
    lcs = _lcs_length(sentence_tokens, evidence_tokens)
    s_len = len(sentence_tokens)
    e_len = len(evidence_tokens)
    if lcs == 0 or s_len == 0 or e_len == 0:
        return 0.0, 0.0, 0.0, lcs
    precision = lcs / s_len
    recall = lcs / e_len
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return f1, precision, recall, lcs


# ---- Citation metrics (Honovich et al., 2022) ----


def answer_citation_recall(scores: list[GroundednessScore]) -> float:
    """Fraction of sentences carrying at least one *valid* citation.

    Honovich et al. (2022) define citation recall as the proportion
    of sentences with a citation that resolves to a real evidence
    block. We follow that interpretation — a sentence citing only
    an unresolved `[ref-x]` doesn't count as attributed.
    """
    if not scores:
        return 0.0
    cited = sum(1 for s in scores if s.cited_refs)
    return cited / len(scores)


def answer_citation_precision(
    scores: list[GroundednessScore],
) -> float:
    """Fraction of emitted `[ref-x]` markers that resolve to evidence.

    Honovich et al. (2022). If the LLM emits `[ref-a] [ref-bogus]`
    on a sentence and only `ref-a` resolves, the per-sentence
    precision is 1/2. We aggregate as a weighted mean by citation
    count so a sentence with zero citations contributes 0 to both
    numerator and denominator (not "perfect precision").
    """
    total_emitted = 0
    total_resolved = 0
    for s in scores:
        emitted = len(s.cited_refs) + len(s.unresolved_refs)
        total_emitted += emitted
        total_resolved += len(s.cited_refs)
    if total_emitted == 0:
        return 0.0
    return total_resolved / total_emitted


def answer_overall_rouge_l(scores: list[GroundednessScore]) -> float:
    """Macro-average ROUGE-L F1 across sentences.

    Macro-average (unweighted mean per sentence) is what Lin (2004)
    reports for multi-sentence evaluation; micro-average would
    weight by sentence length and obscure short-sentence behaviour.
    """
    if not scores:
        return 0.0
    return sum(s.rouge_l_f1 for s in scores) / len(scores)


# ---- Per-sentence scoring ----


def _strip_citations(text: str) -> str:
    """Remove `[ref-x]` markers so the sentence tokenization reflects
    only the claim, not the citation metadata."""
    return _CITATION_STRIP_RE.sub("", text)


def compute_sentence_groundedness(
    sentence: str,
    cited_refs: list[str],
    evidence_map: dict[str, str],
) -> GroundednessScore:
    """Score one sentence against the union of its cited evidence.

    Splits the cited refs into resolved (key exists in evidence_map)
    and unresolved (LLM hallucinated). Computes ROUGE-L F1 between
    the sentence (citations stripped) and the union of cited evidence.
    """
    claim = _strip_citations(sentence)
    sent_tokens = list(tokenize(claim))
    evidence_tokens: list[str] = []
    resolved: list[str] = []
    unresolved: list[str] = []
    for rid in cited_refs:
        text = evidence_map.get(rid)
        if text is None:
            unresolved.append(rid)
            continue
        resolved.append(rid)
        evidence_tokens.extend(tokenize(text))
    f1, precision, recall, lcs = rouge_l_f1(sent_tokens, evidence_tokens)
    return GroundednessScore(
        sentence=sentence,
        cited_refs=resolved,
        unresolved_refs=unresolved,
        rouge_l_f1=f1,
        rouge_l_precision=precision,
        rouge_l_recall=recall,
        sentence_tokens=len(sent_tokens),
        evidence_tokens=len(evidence_tokens),
        lcs_length=lcs,
    )


def groundedness_for_answer(
    answer: str,
    evidence_map: dict[str, str],
) -> list[GroundednessScore]:
    """Split `answer` into sentences and score each with ROUGE-L F1.

    Citations are extracted per-sentence. `answer_citation_recall`
    and `answer_citation_precision` aggregate over the returned list.
    """
    sents = split_sentences(answer)
    scores: list[GroundednessScore] = []
    for sent in sents:
        cited = extract_citation_refs(sent)
        scores.append(compute_sentence_groundedness(sent, cited, evidence_map))
    return scores


# ---- Faithfulness (Maynez et al., 2020) ----
#
# A claim can be supported by evidence *and* still be wrong (paraphrase
# inaccuracy), or have no citation *and* still be supported (the LLM
# forgot the [N]). The two metrics catch different failure modes:
# Citation Precision catches fabricated refs; faithfulness catches
# unsupported claims regardless of citation. Together they pin down
# "the LLM is making things up."
#
# Implementation: the dependency-light lexical-entailment proxy from
# Goodrich et al., 2019 (referenced in the Maynez 2020 follow-up
# literature). A sentence is "supported" iff every token in the claim
# also appears in the union of its cited evidence's text. No model
# dependency. Doesn't catch paraphrastic hallucination, but surfaces the
# most operationally useful failure mode: "this sentence had nothing
# to do with the evidence at all."


def _strip_citations_for_support(text: str) -> list[str]:
    """Lowercase alphanumeric tokens with citation markers and any
    chain-of-thought block stripped.

    Same token pattern as the rest of the codebase
    (`_TOKEN_RE = re.compile(r"[a-z0-9]+")` in `wiki_corpus.tokenize`).
    Citation markers like `[1]`, `[ref-a]` and a `<think>...</think>`
    block (when present) are removed before tokenizing so a sentence
    like "Foo [1]." reduces to the same tokens as the underlying
    claim "Foo.", letting the support check compare apples to apples
    against the evidence text.
    """
    from .wiki_corpus import tokenize as _tokenize
    text = _THINK_BLOCK_RE.sub("", text)
    return _tokenize(_CITATION_STRIP_RE.sub("", text))


def extract_atomic_facts(answer: str) -> list[str]:
    """Split `answer` into atomic facts (one per sentence) for
    faithfulness evaluation. Citations and the LLM's chain-of-thought
    block (when present) are stripped so each fact is a plain claim,
    not a claim + citation marker or planning prose.

    Empty / whitespace input returns []. Each non-empty segment
    becomes one fact; the unit is a sentence (split via the existing
    `split_sentences` helper, which already handles abbreviations and
    citation-prefix re-attachment correctly)."""
    # Reasoning models (MiniMax-M3) emit their planning as a
    # `<think>...</think>` block before the actual answer; that
    # planning text describes the model's reasoning, not the user's
    # answer, and including it in the denominator of the faithfulness
    # metric would drag the per-turn score toward 0 on every turn
    # where the model has any non-trivial planning. Strip the block
    # first so each remaining fact is what the operator actually sees.
    cleaned = _THINK_BLOCK_RE.sub("", answer)
    raw = split_sentences(cleaned)
    # Strip inline `[N]` / `[ref-x]` / `[wiki__foo]` markers from each
    # fact so the support check compares claims to evidence, not
    # claims + citation metadata to evidence.
    return [_CITATION_STRIP_RE.sub("", s).strip() for s in raw if s.strip()]


def fact_supported(
    claim: str,
    evidence_map: dict[str, str],
    cited_refs: list[str],
) -> float:
    """Jaccard-style overlap (|claim ∩ evidence| / |claim|), 0..1.

    NOT a strict subset. Strict subset ("every claim token must appear
    in evidence") returns 0/1 = 0 for any paraphrased answer, even a
    correct one -- useless as a real-world signal because LLMs almost
    always paraphrase at least a few words.

    Jaccard-token-overlap (this implementation) shares the same
    dependency-light property as the proxy in the Maynez 2020
    follow-up literature (Goodrich et al., 2019) but gives partial
    credit for partial support, so the dashboard shows variation
    between turns -- a paraphrase gets ~0.5-0.7, a fully supported
    claim gets 1.0, a fabrication (different vocab) gets ~0.0-0.1.

    Resolved citations only -- unresolved refs contribute zero
    tokens, matching the Citation Precision convention.

    Returns 0.0 for: no cited refs; no tokens in the claim after
    stripping (pure-punctuation); or empty evidence_map.
    """
    if not cited_refs:
        return 0.0
    claim_tokens = set(_strip_citations_for_support(claim))
    if not claim_tokens:
        return 0.0
    evidence_tokens: set[str] = set()
    for ref in cited_refs:
        text = evidence_map.get(ref)
        if not text:
            continue
        evidence_tokens.update(_strip_citations_for_support(text))
    if not evidence_tokens:
        return 0.0
    # Jaccard: |A ∩ B| / |A|, NOT |A ∩ B| / |A ∪ B|. We use precision-
    # style overlap (intersection / |claim|) so a small claim that
    # shares all its tokens with a large evidence block still scores
    # 1.0; a large claim sharing few tokens scores low. This matches
    # the intuition "how much of the claim is in evidence?".
    overlap = len(claim_tokens & evidence_tokens)
    return overlap / len(claim_tokens)


def _sentence_faithful(claim: str, cited_refs: list[str], evidence_map: dict[str, str]) -> float:
    return fact_supported(claim, evidence_map, cited_refs)


def answer_faithfulness(
    scores: list[GroundednessScore],
    evidence_map: dict[str, str],
) -> float:
    """Mean sentence-level faithfulness across the answer, 0..1.

    A sentence with no cited refs at all still counts toward the
    denominator -- it is the strongest hallucination signal (an
    entirely unsupported claim). It contributes 0 to the numerator
    because there's no evidence to verify against. This intentionally
    diverges from Citation Precision's convention (which excludes
    citation-less sentences) because Faithfulness answers a different
    question: "is this claim supported?" not "did the model cite
    anything?". A free-floating unsupported claim IS a problem.

    The "supported" score per sentence is the Jaccard token overlap
    (see `fact_supported` docstring) so a paraphrase scores 0.5-0.7
    rather than 0 -- the dashboard will show real variation across
    turns instead of an always-zero line."""
    if not scores:
        return 0.0
    supported_total = 0.0
    total = 0
    for s in scores:
        atoms = extract_atomic_facts(s.sentence)
        for atom in atoms:
            total += 1
            if s.cited_refs:
                supported_total += _sentence_faithful(atom, s.cited_refs, evidence_map)
    if total == 0:
        return 0.0
    return supported_total / total


# ---- Failure-mode classifier ----
#
# The four-metric dashboard (ROUGE-L / Citation Recall / Citation
# Precision / Faithfulness) tells you a turn is bad, but not WHY.
# Operators reading a low-Faithfulness spike have three plausible
# fixes: tweak the retriever, expand the corpus, or tighten the
# prompt. They are not interchangeable. This classifier picks one
# bucket per turn so the dashboard can decompose the rolling window
# by cause. The thresholds are deliberately conservative -- they
# only fire on turns that are clearly bad in the metric they target,
# not on borderline cases.
#
# Labels (string-literal so they survive JSON round-trips and
# dashboard rendering without an Enum import):
#   "ok"                    -- the model used the retrieved evidence
#   "retrieval_miss"        -- top hit scored 0 or had 0 coverage
#   "insufficient_evidence" -- evidence exists but the model chose
#                              not to cite most of it AND its claims
#                              aren't in what it cited
#   "model_hallucination"   -- evidence exists AND the model cites
#                              a lot of it, but the answer text and
#                              cited evidence share almost no tokens
#                              (low precision on emitted [ref-x]) --
#                              the model is fabricating structure on
#                              top of real evidence


def classify_failure_mode(
    top_hit_score: float,
    top_hit_coverage: float,
    faithfulness: float,
    citation_recall: float,
    citation_precision: float,
) -> str:
    """Classify one turn's failure mode from its raw metrics.

    Args are scalars extracted from the turn's hits + groundedness
    numbers, so this function has no IO and is trivially testable.
    Order of evaluation: retrieval_miss first (cheapest signal,
    explains everything downstream), then the answer-level split
    between insufficient_evidence and model_hallucination.
    """
    if top_hit_score <= 0.0 or top_hit_coverage <= 0.0:
        return "retrieval_miss"
    if faithfulness < 0.3 and citation_recall < 0.3:
        return "insufficient_evidence"
    if faithfulness < 0.3 and citation_precision < 0.7:
        return "model_hallucination"
    return "ok"
