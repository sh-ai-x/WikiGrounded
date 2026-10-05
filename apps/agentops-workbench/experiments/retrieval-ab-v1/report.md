# Retrieval A/B report (split=dev, k=5)

n=8 queries. Metrics are file-level (chunk hits collapsed to their parent via `collapse_to_parents`) so whole-file and chunk-based modes are scored on the same unit.

| mode | Hit@k | R@k | P@k | MRR | nDCG@k | index_s | p50_ms |
|---|---|---|---|---|---|---|---|
| tfidf | 1.000 | 1.000 | 0.463 | 0.812 | 0.954 | 0.00 | 0.0 |
| bm25 | 1.000 | 1.000 | 0.463 | 0.875 | 1.000 | 0.00 | 0.0 |
| dense | 0.750 | 0.750 | 0.392 | 0.500 | 0.651 | 0.10 | 0.0 |
| hybrid | 1.000 | 1.000 | 0.463 | 0.812 | 0.954 | 0.00 | 0.0 |
| hybrid_rerank | 1.000 | 1.000 | 0.463 | 0.875 | 1.000 | 0.00 | 42.5 |

## Delta vs bm25 (baseline)

| mode | ΔHit@k | ΔR@k | ΔP@k | ΔMRR | ΔnDCG@k |
|---|---|---|---|---|---|
| tfidf | +0.000 | +0.000 | +0.000 | -0.062 | -0.046 |
| dense | -0.250 | -0.250 | -0.071 | -0.375 | -0.349 |
| hybrid | +0.000 | +0.000 | +0.000 | -0.062 | -0.046 |
| hybrid_rerank | +0.000 | +0.000 | +0.000 | +0.000 | +0.000 |

## Per-family nDCG@k

| mode | acronym | multi_hop | no_answer | paraphrase | single_hop | synonym |
|---|---|---|---|---|---|---|
| tfidf | 1.000 | 1.000 | 1.000 | 1.000 | 0.815 | 1.000 |
| bm25 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| dense | 1.000 | 0.920 | 1.000 | 0.659 | 0.000 | 0.815 |
| hybrid | 1.000 | 1.000 | 1.000 | 1.000 | 0.815 | 1.000 |
| hybrid_rerank | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

_Caveat: this run's `n` is illustrative, not statistically settled. See docs/adr/0010-dense-retrieval-and-reranking.md for the metrics' documented limitations (Citation Precision/Recall track prompt compliance, not retrieval quality, at the answer-level layer)._
