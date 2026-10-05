"""Rolling-window metrics aggregator for the wiki chat surface.

Exposes two complementary views the dashboard needs:

  1. Latency (`StageLatencyRecorder`) -- per-call wall-clock for each
     stage of a wiki search (tokenize, score, sort, total). The
     recorder keeps the last `WINDOW` samples per stage and exposes
     p50/p95/count, computed in O(N) over the window per call
     (small window, called on demand by the dashboard's poll).
  2. Groundedness (`GroundednessRecorder`) -- per-call ROUGE-L F1,
     Citation Recall, Citation Precision. Same window shape. Means
     over the window -- a per-call mean across the trailing window
     tells the operator whether the model is drifting in aggregate,
     which the per-turn score on the chat transcript alone can't show.

Why rolling windows and not lifetime aggregates: token costs
compound, indexes warm up, providers throttle -- lifetime numbers
are dominated by early outliers and the dashboard would appear
to never recover even after a fix. A trailing window gives an
honest "what is this thing doing right now?" view.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

WINDOW = 200  # last N samples per stage / per metric


@dataclass(frozen=True)
class StageSample:
    tokenize_ms: float
    score_ms: float
    sort_and_return_ms: float
    total_ms: float


class StageLatencyRecorder:
    """Per-stage latency samples, last `WINDOW`."""

    def __init__(self, window: int = WINDOW) -> None:
        self._lock = threading.Lock()
        self._window = window
        self._tokenize: deque[float] = deque(maxlen=window)
        self._score: deque[float] = deque(maxlen=window)
        self._sort: deque[float] = deque(maxlen=window)
        self._total: deque[float] = deque(maxlen=window)

    def record(self, sample: StageSample) -> None:
        with self._lock:
            self._tokenize.append(sample.tokenize_ms)
            self._score.append(sample.score_ms)
            self._sort.append(sample.sort_and_return_ms)
            self._total.append(sample.total_ms)

    def stats(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                "tokenize_ms": _percentiles(self._tokenize),
                "score_ms": _percentiles(self._score),
                "sort_and_return_ms": _percentiles(self._sort),
                "total_ms": _percentiles(self._total),
            }

    def reset_for_tests(self) -> None:
        with self._lock:
            self._tokenize.clear()
            self._score.clear()
            self._sort.clear()
            self._total.clear()


@dataclass(frozen=True)
class GroundednessSample:
    rouge_l_f1: float
    citation_recall: float
    citation_precision: float
    # Mean sentence-level faithfulness (Maynez et al., 2020) for this
    # turn, 0..1. Computed via the lexical-entailment proxy in
    # `groundedness.answer_faithfulness`. Default 0.0 so older tests
    # (and any caller that doesn't yet know about the metric) keep
    # working without a forced update.
    faithfulness: float = 0.0
    # One of "ok" | "retrieval_miss" | "insufficient_evidence" |
    # "model_hallucination". See `groundedness.classify_failure_mode`.
    # Default "ok" so older callers and tests keep working.
    failure_mode: str = "ok"


class GroundednessRecorder:
    """Per-call groundedness averages across the trailing window.

    Per-call samples are deterministic; the aggregator is a plain
    running mean over the last `WINDOW` calls. Means (not p50/p95)
    because each call already emits one number per metric -- a single
    mean answers "is the model drifting?" without needing a
    distribution.
    """

    # Failure-mode labels the rolling dashboard can decompose by.
    # `dict[str, int]` shape keeps the API response schema stable even
    # when a new label is added -- new keys appear, old keys stay.
    _FAILURE_MODES: tuple[str, ...] = (
        "ok",
        "retrieval_miss",
        "insufficient_evidence",
        "model_hallucination",
    )

    def __init__(self, window: int = WINDOW) -> None:
        self._lock = threading.Lock()
        self._window = window
        self._rouge: deque[float] = deque(maxlen=window)
        self._recall: deque[float] = deque(maxlen=window)
        self._precision: deque[float] = deque(maxlen=window)
        self._faithfulness: deque[float] = deque(maxlen=window)
        self._failure_counts: dict[str, int] = {m: 0 for m in self._FAILURE_MODES}

    def record(self, sample: GroundednessSample) -> None:
        with self._lock:
            self._rouge.append(sample.rouge_l_f1)
            self._recall.append(sample.citation_recall)
            self._precision.append(sample.citation_precision)
            self._faithfulness.append(sample.faithfulness)
            mode = sample.failure_mode
            if mode not in self._failure_counts:
                # Unknown label (forward-compat): count it under "ok" so
                # the dashboard doesn't inflate the other buckets.
                mode = "ok"
            self._failure_counts[mode] += 1

    def stats(self) -> dict[str, float]:
        with self._lock:
            n = len(self._rouge)
            out: dict[str, float] = {
                "sample_count": n,
                "rouge_l_f1_avg": _mean(self._rouge),
                "citation_recall_avg": _mean(self._recall),
                "citation_precision_avg": _mean(self._precision),
                "faithfulness_avg": _mean(self._faithfulness),
            }
            # Counts + fractions for the failure-mode breakdown panel.
            for mode in self._FAILURE_MODES:
                c = self._failure_counts.get(mode, 0)
                out[f"failure_mode_{mode}_count"] = c
                out[f"failure_mode_{mode}_frac"] = (c / n) if n else 0.0
            return out

    def reset_for_tests(self) -> None:
        with self._lock:
            self._rouge.clear()
            self._recall.clear()
            self._precision.clear()
            self._faithfulness.clear()
            for mode in self._FAILURE_MODES:
                self._failure_counts[mode] = 0


def _percentiles(samples: deque[float]) -> dict[str, float]:
    """Return {count, p50, p95} for a deque of ms samples.

    Edge cases: empty -> all zeros; single sample -> both percentiles
    equal that sample. Both definitions are required -- a p50 without
    p95, or a p95 below p50, would be a bug, so the test suite asserts
    `p95 >= p50`.
    """
    n = len(samples)
    if n == 0:
        return {"count": 0, "p50": 0.0, "p95": 0.0}
    # bisect.insort on a sorted copy keeps O(N log N) per call --
    # negligible for WINDOW=200, and the simplest correct
    # implementation that doesn't depend on numpy.
    sorted_samples = sorted(samples)
    return {
        "count": n,
        "p50": _percentile(sorted_samples, 0.50),
        "p95": _percentile(sorted_samples, 0.95),
    }


def _percentile(sorted_samples: list[float], p: float) -> float:
    """Linear interpolation percentile (NIST/excel "PERCENTILE.INC").

    p in 0..1. `sorted_samples` is already non-decreasing.
    """
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    rank = p * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


def _mean(samples: deque[float]) -> float:
    n = len(samples)
    if n == 0:
        return 0.0
    return sum(samples) / n
