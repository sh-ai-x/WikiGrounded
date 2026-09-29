"use client";

import { useEffect, useRef, useState } from "react";

// Shape returned by GET /v1/wiki/metrics. The frontend is permissive
// about extra fields -- only the ones rendered here are required,
// anything else is ignored (so adding new metrics server-side doesn't
// break this component).
type LatencyStage = { p50: number; p95: number; count: number };
type LatencyStats = {
  tokenize_ms: LatencyStage;
  score_ms: LatencyStage;
  sort_and_return_ms: LatencyStage;
  total_ms: LatencyStage;
};
type GroundednessStats = {
  sample_count: number;
  rouge_l_f1_avg: number;
  citation_recall_avg: number;
  citation_precision_avg: number;
  // Maynez et al., 2020 -- see faithfulness badge on each turn.
  faithfulness_avg: number;
};
type MetricsResponse = {
  latency: LatencyStats;
  groundedness: GroundednessStats;
};

type PollInterval = 5 | 30 | 60;

function pct(value: number): string {
  // Percentages, clamped to 0..100. Empty input -> "—".
  if (!Number.isFinite(value)) return "—";
  return `${Math.max(0, Math.min(100, Math.round(value * 100)))}%`;
}

function ms(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(1)} ms`;
}

function metricColor(value: number): string {
  // Same threshold curve as the per-turn groundedness badges -- green /
  // amber / red, so the color story is consistent across the page.
  if (value >= 0.6) return "#16a34a";
  if (value >= 0.3) return "#d97706";
  return "#dc2626";
}

function Stat({ label, value, color, title }: { label: string; value: string; color?: string; title?: string }) {
  return (
    <div className="metric-stat">
      <div className="metric-stat-label">{label}</div>
      <div className="metric-stat-value" title={title} style={color ? { color } : undefined}>
        {value}
      </div>
    </div>
  );
}

function Bar({ value }: { value: number }) {
  const width = Math.max(0, Math.min(100, value * 100));
  return (
    <div className="metric-bar-track">
      <div className="metric-bar-fill" style={{ width: `${width}%`, backgroundColor: metricColor(value) }} />
    </div>
  );
}

export default function MetricsPanel({
  bearer,
  retrieval,
}: {
  bearer: string;
  // ADR-0010 §4.5: retrieval mode is fixed per corpus at index time and
  // this window aggregates whatever mode was active when each answer was
  // produced -- so the subtitle names it, rather than presenting mixed-mode
  // numbers as one undifferentiated figure. `null` before any corpus is
  // indexed.
  retrieval?: string | null;
}) {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [interval, setInterval_] = useState<PollInterval>(5);
  // The metrics recorders are process-wide singletons on the backend,
  // so a long-running dev server keeps accumulating samples across
  // browser refreshes -- a new tab would otherwise inherit whatever
  // the previous session left in the rolling window. Fire a one-time
  // reset the moment a bearer becomes available (before the first
  // poll) so each fresh page load starts its own window at zero.
  const resetSentRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    async function tick() {
      // No bearer -> waiting for auth, not an error. Real prod-mode
      // deployments have a real login flow; this is the dev/demo path
      // and the user's job here is just to paste a token once.
      if (!bearer) {
        if (!cancelled) setError("awaiting auth (paste a Bearer token)");
        return;
      }
      if (!resetSentRef.current) {
        resetSentRef.current = true;
        try {
          await fetch("/api/v1/wiki/metrics/reset", {
            method: "POST",
            headers: { Authorization: `Bearer ${bearer}` },
          });
        } catch {
          // Non-fatal: worst case this tab shows a stale window
          // instead of a fresh one. The poll below still runs.
        }
      }
      try {
        const r = await fetch("/api/v1/wiki/metrics", {
          headers: { Authorization: `Bearer ${bearer}` },
        });
        if (!r.ok) {
          if (!cancelled) setError(`metrics unavailable (HTTP ${r.status})`);
          return;
        }
        if (cancelled) return;
        setData(await r.json());
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError((err as Error).message);
      }
    }
    tick();
    const handle = window.setInterval(tick, interval * 1000);
    return () => {
      cancelled = true;
      window.clearInterval(handle);
    };
  }, [interval, bearer]);

  return (
    <section className="card">
      <div className="toolbar">
        <h2 style={{ margin: 0 }}>3. Metrics</h2>
        <span className="muted">polling every {interval}s</span>
        <button
          type="button"
          onClick={() => setInterval_((i) => (i === 5 ? 30 : i === 30 ? 60 : 5))}
        >
          {interval === 5 ? "30s" : interval === 30 ? "60s" : "5s"}
        </button>
      </div>

      {error && (
        <p className="status" style={{ color: "var(--accent)" }}>
          metrics unavailable: {error}
        </p>
      )}

      <h3>Accuracy / hallucination (recent window)</h3>
      <p className="muted">
        Mean over the last {data?.groundedness.sample_count ?? 0} /v1/wiki/qa answers
        {retrieval && (
          <>
            {" "}(retrieval: <strong>{retrieval}</strong>)
          </>
        )}
        . A drop here means the model is starting to drift off the evidence.
      </p>
      {data && (
        <div className="metric-stats-row">
          <Stat
            label="Citation Precision"
            value={pct(data.groundedness.citation_precision_avg)}
            color={metricColor(data.groundedness.citation_precision_avg)}
          />
          <Bar value={data.groundedness.citation_precision_avg} />
          <Stat
            label="Citation Recall"
            value={pct(data.groundedness.citation_recall_avg)}
            color={metricColor(data.groundedness.citation_recall_avg)}
          />
          <Bar value={data.groundedness.citation_recall_avg} />
          <Stat
            label="ROUGE-L F1 (avg)"
            value={pct(data.groundedness.rouge_l_f1_avg)}
            color={metricColor(data.groundedness.rouge_l_f1_avg)}
          />
          <Bar value={data.groundedness.rouge_l_f1_avg} />
          <Stat
            label="Faithfulness (avg)"
            value={pct(data.groundedness.faithfulness_avg)}
            color={metricColor(data.groundedness.faithfulness_avg)}
            title="Maynez et al., 2020: fraction of answer sentences whose tokens appear in the cited evidence. Independent of citations -- catches unsupported claims even when the LLM cites a real [N] but paraphrases inaccurately."
          />
          <Bar value={data.groundedness.faithfulness_avg} />
        </div>
      )}

      <h3>Latency (ms, recent window)</h3>
      <p className="muted">
        Per-stage timing of /v1/wiki/search. p50 is the median request,
        p95 is the slow tail. If a stage's p95 dominates total p95,
        look there first.
      </p>
      {data && (
        <table className="metric-table">
          <thead>
            <tr>
              <th>stage</th>
              <th>p50</th>
              <th>p95</th>
              <th>samples</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>tokenize</td>
              <td>{ms(data.latency.tokenize_ms.p50)}</td>
              <td>{ms(data.latency.tokenize_ms.p95)}</td>
              <td>{data.latency.tokenize_ms.count}</td>
            </tr>
            <tr>
              <td>score</td>
              <td>{ms(data.latency.score_ms.p50)}</td>
              <td>{ms(data.latency.score_ms.p95)}</td>
              <td>{data.latency.score_ms.count}</td>
            </tr>
            <tr>
              <td>sort + return</td>
              <td>{ms(data.latency.sort_and_return_ms.p50)}</td>
              <td>{ms(data.latency.sort_and_return_ms.p95)}</td>
              <td>{data.latency.sort_and_return_ms.count}</td>
            </tr>
            <tr style={{ fontWeight: 600 }}>
              <td>total</td>
              <td>{ms(data.latency.total_ms.p50)}</td>
              <td>{ms(data.latency.total_ms.p95)}</td>
              <td>{data.latency.total_ms.count}</td>
            </tr>
          </tbody>
        </table>
      )}
    </section>
  );
}
