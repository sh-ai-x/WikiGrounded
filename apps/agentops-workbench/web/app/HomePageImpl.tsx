"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import MetricsPanel from "./MetricsPanel";

// AC3 trust-field types — must match backend `WikiHit.to_dict()`.
type Hit = {
  ref_id: string;
  title: string;
  score: number;
  source_path: string;
  evidence_span: string;
  match_offsets: [number, number][];
  coverage: number;
  contributing_terms: string[];
  mtime: number;
  // Set only when the indexed directory was an Obsidian vault (browser
  // supplied vault_name on upload). One-click "open in vault" link.
  obsidian_uri?: string | null;
};

type SentenceScore = {
  sentence: string;
  cited_refs: string[];
  unresolved_refs: string[];
  // Three published metrics per sentence (Lin 2004 + Honovich 2022).
  rouge_l_f1: number;
  rouge_l_precision: number;
  rouge_l_recall: number;
  sentence_tokens: number;
  evidence_tokens: number;
  lcs_length: number;
};

type QaResponse = {
  query: string;
  corpus_id: string;
  answer: string;
  // Echoed back so the client can rejoin the conversation on a
  // follow-up turn. Server mints one if `body.thread_id` is None.
  thread_id: string;
  overall_rouge_l_f1: number;
  citation_recall: number;
  citation_precision: number;
  // Maynez et al., 2020: see chat transcript badge. Independent of
  // citation metrics -- catches unsupported claims even when the
  // LLM cites a real [N] but paraphrases inaccurately.
  faithfulness: number;
  sentences: SentenceScore[];
  hits: Hit[];
};

// One turn in the chat transcript. Each assistant turn carries the
// metrics + per-sentence grounding the operator can drill into.
type ChatMessage =
  | { role: "user"; content: string }
  | {
      role: "assistant";
      content: string;
      hits: Hit[];
      sentences: SentenceScore[];
      overall_rouge_l_f1: number;
      citation_recall: number;
      citation_precision: number;
      // Maynez et al., 2020: fraction of answer sentences whose tokens
      // appear in the cited evidence. Catches unsupported claims that
      // pass Citation Precision (which only checks whether cited refs
      // resolve to real evidence, not whether the underlying claim is
      // actually supported by them).
      faithfulness: number;
      // Captured at send time, not read live -- so a turn always shows
      // which mode actually produced ITS answer, even if the operator
      // switches either dropdown before the next turn.
      retrieval: RetrievalMode;
      provider: Provider;
    };

// ADR-0010: tfidf/bm25 are the original lexical modes; dense/hybrid/
// hybrid_rerank are CPU-only additions (a small ONNX embedding model +
// optional cross-encoder rerank) -- see README's "Retrieval algorithm"
// section and docs/adr/0010-dense-retrieval-and-reranking.md.
type RetrievalMode = "tfidf" | "bm25" | "dense" | "hybrid" | "hybrid_rerank";

// LLM provider for the chat/answer call (independent of retrieval mode
// above, which only affects search). API keys are never sent to or read
// by the browser -- they live server-side in `.env`; the picker only
// selects which server-side key gets used. `GET /v1/wiki/providers`
// reports which of these actually have a key configured.
type Provider = "local-fake" | "minimax" | "openai";

type ProvidersResponse = {
  available: Provider[];
  default: Provider;
};

type IndexResponse = {
  corpus_id: string;
  doc_count: number;
  duration_ms: number;
  // The mode that actually got resolved (server-side default applies when
  // the request omitted `retrieval`) -- used to attribute the metrics
  // dashboard below to the mode that produced its numbers.
  retrieval: RetrievalMode;
};

// Mirrors the server's SKIP_DIR_NAMES so client and server agree on
// what to ignore when walking the picked directory.
const SKIP_DIR_NAMES = new Set([
  ".git", ".hg", ".svn", ".obsidian", ".dev-kit", ".claude", ".codex",
  ".serena", ".gemini", ".metagraph", ".trash", ".worktrees", ".venv",
  ".pytest_cache", ".ruff_cache", ".mypy_cache", ".idea", ".vscode",
  "__pycache__", "node_modules", "_flat",
]);

// Recursive walk + junk-skip + .md read. Browser-side mirror of
// `collect_wiki_files` on the server, so what the user sees is what
// gets indexed. Also peeks for a `.obsidian/` subdirectory (which we
// skip from the file list but use to detect "this is an Obsidian
// vault" so the upload can carry `vault_name`).
async function walkPickedDir(
  root: FileSystemDirectoryHandle,
): Promise<{
  files: { path: string; content: string; mtime: number }[];
  hasObsidian: boolean;
}> {
  const out: { path: string; content: string; mtime: number }[] = [];
  let hasObsidian = false;

  async function visit(
    dir: FileSystemDirectoryHandle,
    prefix: string,
  ): Promise<void> {
    // The directory iterator is async; await each entry.
    // @ts-expect-error -- FileSystemDirectoryHandle.values() types missing in some TS libs
    for await (const entry of dir.values()) {
      if (entry.kind === "directory") {
        if (entry.name === ".obsidian" && prefix === "") {
          hasObsidian = true;
          // Don't recurse -- Obsidian's plugin/config dir isn't content.
          continue;
        }
        if (SKIP_DIR_NAMES.has(entry.name) || entry.name.startsWith(".")) {
          continue;
        }
        const sub = await dir.getDirectoryHandle(entry.name);
        await visit(sub, prefix ? `${prefix}/${entry.name}` : entry.name);
      } else if (entry.kind === "file") {
        if (!entry.name.toLowerCase().endsWith(".md")) continue;
        if (entry.name.startsWith(".")) continue;
        const fh = await dir.getFileHandle(entry.name);
        const file = await fh.getFile();
        const content = await file.text();
        out.push({
          path: prefix ? `${prefix}/${entry.name}` : entry.name,
          content,
          mtime: file.lastModified,
        });
      }
    }
  }

  await visit(root, "");
  return { files: out, hasObsidian };
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Build a snippet with `<mark>` overlays around the matched term
// positions. The server hands us `evidence_span` + `match_offsets`
// (character positions into the full document); we render the
// substring with `<mark>` tags at those positions.
function highlightSpan(span: string, fullDoc: string, offsets: [number, number][], spanStart: number): React.ReactNode {
  if (!offsets.length) return span;
  // Translate offsets-in-doc to offsets-in-span. We don't know the
  // exact spanStart value the server used, so we approximate by
  // finding each matched term in the span text.
  const tokens = new Set<string>();
  for (const [s, e] of offsets) {
    if (s >= spanStart && e <= spanStart + span.length) {
      tokens.add(fullDoc.slice(s, e));
    }
  }
  if (tokens.size === 0) return span;
  const parts: React.ReactNode[] = [];
  const re = new RegExp(
    Array.from(tokens).map(escapeRegExp).sort((a, b) => b.length - a.length).join("|"),
    "gi",
  );
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(span)) !== null) {
    if (match.index > lastIndex) {
      parts.push(span.slice(lastIndex, match.index));
    }
    parts.push(<mark key={key++}>{match[0]}</mark>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < span.length) {
    parts.push(span.slice(lastIndex));
  }
  return <>{parts}</>;
}

// Color the score: green >= 0.6, amber 0.3-0.6, red < 0.3.
function groundednessColor(score: number): string {
  if (score >= 0.6) return "#16a34a";
  if (score >= 0.3) return "#d97706";
  return "#dc2626";
}

// Reference copy for the two metric families this page shows. Kept as
// data (not scattered prose) so "search" and "qa" can render the same
// entries consistently and so the numbers are never explained differently
// in two places.
// The "score" doc entry is mode-specific -- tfidf/bm25/dense/hybrid/
// hybrid_rerank each put it on a different scale (ADR-0010 Consequences),
// so it's computed per-mode rather than stated as one fixed fact.
function scoreMetricDoc(retrieval: RetrievalMode): { label: string; meaning: string; why: string } {
  switch (retrieval) {
    case "bm25":
      return {
        label: "score",
        meaning:
          "Okapi BM25 score (unbounded, not 0–1). Higher = more relevant; saturates a repeated term instead of scoring it near-linearly.",
        why:
          "BM25 also normalizes for document length, so a short note and a long one are compared fairly on the same term.",
      };
    case "dense":
      return {
        label: "score",
        meaning:
          "Cosine similarity between your query and this chunk's embedding, 0–1. Higher = more semantically similar.",
        why:
          "Captures meaning even when the wording differs entirely (a synonym or a paraphrase) — something lexical (TF-IDF/BM25) search cannot do.",
      };
    case "hybrid":
      return {
        label: "score",
        meaning:
          "Reciprocal Rank Fusion (RRF) score combining a lexical (BM25) and a semantic (dense) ranking, typically ~0.01–0.03. Higher = more relevant — not a similarity percentage.",
        why:
          "Combines exact-term matching with semantic matching so a query can find a document via either path.",
      };
    case "hybrid_rerank":
      return {
        label: "score",
        meaning:
          "Cross-encoder relevance score after re-ranking hybrid's top candidates, 0–1. Higher = more relevant.",
        why:
          "A cross-encoder reads the query and the chunk together, which is more precise than scoring them separately — at extra per-query latency.",
      };
    case "tfidf":
    default:
      return {
        label: "score",
        meaning:
          "TF-IDF-weighted cosine similarity between your query and this document, 0–1. Higher = more relevant.",
        why:
          "TF-IDF + cosine is a standard, well-understood retrieval technique — not a bespoke heuristic — so it needs no embedding service and the number means the same thing anyone else measuring TF-IDF similarity would get.",
      };
  }
}

const METRIC_DOCS: Record<
  "search" | "qa",
  { label: string; meaning: string; why: string }[]
> = {
  search: [
    {
      label: "coverage",
      meaning:
        "% of the distinct words in your query that were actually found in this document (matched terms ÷ total query terms).",
      why:
        "score can be high from a few heavily-weighted rare terms even if most of your question isn't in the document. coverage is the plain-English check on that: it tells you how much of what you literally typed the document touches.",
    },
    {
      label: "terms",
      meaning:
        "The words in this document that contributed the most to its score — i.e. why it surfaced.",
      why: "Lets you sanity-check a match without opening the source file.",
    },
  ],
  qa: [
    {
      label: "ROUGE-L F1",
      meaning:
        "Overlap between an answer sentence and its cited evidence, via the Longest Common Subsequence (LCS) of tokens (Lin, 2004). Precision = LCS ÷ sentence tokens, Recall = LCS ÷ evidence tokens.",
      why:
        "The standard published metric for summary/answer-vs-source overlap — it rewards matching word order, not just shared vocabulary — used instead of a custom string-similarity function so the score means something outside this app.",
    },
    {
      label: "Citation Recall",
      meaning:
        "Fraction of answer sentences that carry at least one citation resolving to real evidence.",
      why:
        "Answers \"did the LLM even try to attribute its claims?\" (Honovich et al., 2022 — TRUE).",
    },
    {
      label: "Citation Precision",
      meaning:
        "Fraction of the [ref-x] markers the LLM emitted that actually point to real evidence, vs. fabricated.",
      why:
        "Answers \"can you trust the citations it gave you?\" (Honovich et al., 2022 — TRUE). Recall and Precision are independent: an answer can cite everything (100% recall) while citing the wrong thing (low precision).",
    },
  ],
};

// FastAPI's HTTPException body is `{"detail": "..."}`. Surface just that
// string when present -- e.g. the LLM-provider-error messages server.py
// mints for quota/rate-limit/auth/network failures (see llm/errors.py) --
// rather than dumping the raw JSON blob into the error banner.
async function extractErrorDetail(r: Response): Promise<string> {
  const text = await r.text();
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail) {
      return parsed.detail;
    }
  } catch {
    // Not JSON -- fall through to the raw text below.
  }
  return `${r.status} ${text}`;
}

function MetricsGuide({
  topic,
  retrieval = "tfidf",
}: {
  topic: "search" | "qa";
  retrieval?: RetrievalMode;
}) {
  const rows =
    topic === "search" ? [scoreMetricDoc(retrieval), ...METRIC_DOCS.search] : METRIC_DOCS.qa;
  return (
    <details className="metrics-guide">
      <summary>What do these numbers mean?</summary>
      <dl>
        {rows.map((m) => (
          <div className="metrics-guide-row" key={m.label}>
            <dt>{m.label}</dt>
            <dd>
              {m.meaning}
              <span className="metrics-guide-why">Why this metric: {m.why}</span>
            </dd>
          </div>
        ))}
      </dl>
      {topic === "qa" && (
        <p className="metrics-guide-note">
          All three at 0% usually means the LLM found no supporting evidence
          and explicitly refused to answer rather than guess — that's the
          safe outcome the prompt asks for, not a broken search.
        </p>
      )}
    </details>
  );
}

export default function HomePageImpl() {
  // Initial bearer: env-baked (NEXT_PUBLIC_AGENTOPS_DEV_BEARER) wins,
  // else fall through to dev-mint probe below. setBearer stays callable
  // so the user can still paste a different token.
  const [bearer, setBearer] = useState(
    process.env.NEXT_PUBLIC_AGENTOPS_DEV_BEARER ?? "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authMode, setAuthMode] = useState<
    | { mode: "manual" }
    | { mode: "dev"; principal: string; expiresIn: number }
  | null
  >(null);
  // Corpus state (after picker).
  const [corpusId, setCorpusId] = useState<string | null>(null);
  const [docCount, setDocCount] = useState<number | null>(null);
  const [indexDurationMs, setIndexDurationMs] = useState<number | null>(null);
  // True when the picked directory's root contains a `.obsidian/`
  // subdirectory AND the server got a vault_name with the upload --
  // in which case every hit carries an `obsidian_uri` deep link.
  // Resets on every new directory pick.
  const [isObsidianVault, setIsObsidianVault] = useState(false);
  const [topK, setTopK] = useState(5);
  // Retrieval algorithm for the NEXT directory pick -- see README's
  // "Retrieval algorithm" section. Applies at index time; switching
  // this after a directory is already picked has no effect until the
  // next pick (re-indexing doesn't happen automatically).
  const [retrieval, setRetrieval] = useState<RetrievalMode>("tfidf");
  // The mode actually resolved by the last index-files call -- may differ
  // from `retrieval` above if the server fell back to its own default.
  // Drives the metrics dashboard's mode-attribution subtitle.
  const [resolvedRetrieval, setResolvedRetrieval] = useState<RetrievalMode>("tfidf");
  // LLM provider for the chat call. `null` until `/v1/wiki/providers`
  // responds, at which point it's set to the server's configured default
  // -- so a picker render always reflects a real, key-backed choice.
  const [provider, setProvider] = useState<Provider | null>(null);
  const [availableProviders, setAvailableProviders] = useState<Provider[]>(["local-fake"]);
  // Per-provider live status, keyed by provider name. `undefined` = never
  // checked (a manual click on the status dot triggers the real test call
  // -- POST /v1/wiki/providers/{provider}/check spends a small amount of
  // real money on that provider, so this is never automatic).
  const [providerStatus, setProviderStatus] = useState<
    Record<string, "checking" | "ok" | "unconfigured" | "quota_exceeded" | "rate_limited" | "auth_failed" | "unavailable" | "unknown">
  >({});
  // Chat state -- multi-turn transcript.
  // `messages` holds the full conversation so the operator can scroll
  // back through prior turns; `chatInput` is the unsent draft. `threadId`
  // is server-minted on the first turn and threaded through every
  // follow-up so the LangGraph checkpointer
  // (`graph/wiki_chat.py`) restores conversation history on the
  // server side. Picking a new directory resets the conversation.
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [threadId, setThreadId] = useState<string | null>(null);
  const [expandedTurns, setExpandedTurns] = useState<Set<number>>(new Set());
  // Lets "Cancel" abort an in-flight /v1/wiki/qa call. Not state -- it
  // doesn't need to trigger a re-render, only `busy` does that.
  const chatAbortRef = useRef<AbortController | null>(null);

  const authHeaders = useCallback(
    (): Record<string, string> => (bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    [bearer],
  );

  // On mount: probe /v1/auth/dev-mode and, if enabled, auto-mint a
  // token via /v1/auth/dev-token. Production deployments have
  // AGENTOPS_ALLOW_DEV_TOKEN unset -> this is a no-op and the user
  // falls through to the manual Bearer field below.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/v1/auth/dev-mode");
        if (!r.ok) return;
        const info = (await r.json()) as {
          enabled: boolean;
          default_principal: string;
        };
        if (!info.enabled) return;
        const tk = await fetch(
          `/api/v1/auth/dev-token?principal_id=${encodeURIComponent(info.default_principal)}`,
        );
        if (!tk.ok) return;
        const body = (await tk.json()) as {
          token: string;
          principal_id: string;
          expires_in: number;
        };
        if (cancelled) return;
        setBearer(body.token);
        setAuthMode({
          mode: "dev",
          principal: body.principal_id,
          expiresIn: body.expires_in,
        });
      } catch {
        // silent — dev-mode probe failure just means we're in
        // manual auth mode.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Once authenticated, ask the server which LLM providers actually have
  // a key configured (`local-fake` always does) so the picker never offers
  // a choice that would just 400 on the first chat turn.
  useEffect(() => {
    if (!bearer) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/v1/wiki/providers", {
          headers: { Authorization: `Bearer ${bearer}` },
        });
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as ProvidersResponse;
        setAvailableProviders(data.available);
        setProvider((prev) => prev ?? data.default);
      } catch {
        // silent -- picker just falls back to local-fake only.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [bearer]);

  // ----- AC1: directory picker -----
  async function onPickDirectory() {
    setError(null);
    if (typeof window === "undefined" || !("showDirectoryPicker" in window)) {
      setError(
        "Your browser does not support the File System Access API. " +
        "Please use a Chromium-based browser (Chrome, Edge, Brave, Arc).",
      );
      return;
    }
    setBusy(true);
    try {
      // The picker triggers the OS permission prompt. On approval
      // we get a handle; on denial it throws an AbortError we
      // surface as a friendly message.
      // @ts-expect-error -- showDirectoryPicker is not in lib.dom typings universally
      const root: FileSystemDirectoryHandle = await window.showDirectoryPicker({ mode: "read" });
      const { files, hasObsidian } = await walkPickedDir(root);
      if (files.length === 0) {
        setError(
          "No .md files found under the picked directory (junk dirs " +
          "skipped). Pick a directory that contains markdown notes.",
        );
        return;
      }
      // POST the files to /v1/wiki/index-files. Auto-detected
      // Obsidian vault -> send `vault_name` so the server stamps
      // `obsidian_uri` on every hit and the References tab becomes
      // one-click "open in vault" links.
      const uploadBody: {
        files: typeof files;
        vault_name?: string;
        retrieval: RetrievalMode;
      } = { files, retrieval };
      if (hasObsidian) {
        uploadBody.vault_name = root.name ?? "vault";
      }
      const r = await fetch("/api/v1/wiki/index-files", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(uploadBody),
      });
      if (!r.ok) throw new Error(await extractErrorDetail(r));
      const idx = (await r.json()) as IndexResponse;
      setCorpusId(idx.corpus_id);
      setDocCount(idx.doc_count);
      setIndexDurationMs(idx.duration_ms);
      setResolvedRetrieval(idx.retrieval);
      // Surface a one-line "obsidian vault detected" hint so the
      // operator knows hits will be Obsidian deep links rather than
      // generic file:// paths.
      setIsObsidianVault(hasObsidian);
      // Picking a new directory resets the conversation -- a fresh
      // corpus is a fresh thread.
      setMessages([]);
      setIsObsidianVault(false);
      setChatInput("");
      setThreadId(null);
    } catch (err) {
      const e = err as Error & { name?: string };
      if (e.name === "AbortError") {
        setError(null);
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  // ----- AC3: Chat (multi-turn, threaded via LangGraph checkpointer) -----
  async function onSend(e: React.FormEvent) {
    e.preventDefault();
    const text = chatInput.trim();
    if (!text || !bearer || !corpusId || busy) return;
    // Captured now, not read live later -- if `provider` omitted, the
    // server falls back to its own configured default, so mirror that
    // here for the badge rather than showing a blank/null provider.
    const sentProvider: Provider = provider ?? "local-fake";
    setBusy(true);
    setError(null);
    // Optimistically append the user turn so the UI updates immediately,
    // then post the question to the server. thread_id is sent only
    // when we already have one (i.e. this isn't the first turn) -- the
    // server mints one on the first call and echoes it back; we
    // thread it on every follow-up. The client never resends the
    // transcript; the LangGraph MemorySaver in `graph/wiki_chat.py`
    // restores the conversation history server-side.
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
    ]);
    setChatInput("");
    const controller = new AbortController();
    chatAbortRef.current = controller;
    try {
      const r = await fetch("/api/v1/wiki/qa", {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          corpus_id: corpusId,
          query: text,
          top_k: topK,
          ...(threadId ? { thread_id: threadId } : {}),
          ...(provider ? { provider } : {}),
        }),
        signal: controller.signal,
      });
      if (!r.ok) throw new Error(await extractErrorDetail(r));
      const data = (await r.json()) as QaResponse;
      setThreadId(data.thread_id);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: data.answer,
          hits: data.hits,
          sentences: data.sentences,
          overall_rouge_l_f1: data.overall_rouge_l_f1,
          citation_recall: data.citation_recall,
          citation_precision: data.citation_precision,
          faithfulness: data.faithfulness,
          retrieval: resolvedRetrieval,
          provider: sentProvider,
        },
      ]);
    } catch (err) {
      const e2 = err as Error & { name?: string };
      if (e2.name === "AbortError") {
        // User hit Cancel -- no error banner, no assistant turn. A plain
        // fetch abort only stops the CLIENT from waiting; it doesn't
        // propagate to the server, so the LangGraph turn may still finish
        // in the background and land in that thread's history. Good
        // enough for "stop waiting and let me ask something else" --
        // true mid-generation interruption would need a streaming
        // endpoint, which /v1/wiki/qa isn't.
        setError(null);
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
      chatAbortRef.current = null;
    }
  }

  function onCancelSend() {
    chatAbortRef.current?.abort();
  }

  // Manual, explicit trigger only -- this makes one real test call against
  // the provider's API (a few tokens), so it must never fire automatically
  // (on mount, on an interval, etc.).
  async function checkProviderStatus(p: Provider) {
    setProviderStatus((prev) => ({ ...prev, [p]: "checking" }));
    try {
      const r = await fetch(`/api/v1/wiki/providers/${p}/check`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!r.ok) {
        setProviderStatus((prev) => ({ ...prev, [p]: "unknown" }));
        return;
      }
      const data = (await r.json()) as { provider: string; status: string };
      setProviderStatus((prev) => ({ ...prev, [p]: data.status as (typeof prev)[string] }));
    } catch {
      setProviderStatus((prev) => ({ ...prev, [p]: "unknown" }));
    }
  }

  function providerStatusColor(status: string | undefined): string {
    if (status === "ok") return "#16a34a";
    if (status === "checking") return "#9ca3af";
    if (status === undefined) return "#d1d5db";
    return "#dc2626"; // unconfigured / quota_exceeded / rate_limited / auth_failed / unavailable / unknown
  }

  function providerStatusLabel(status: string | undefined): string {
    if (status === undefined) return "not checked yet — click to test";
    const labels: Record<string, string> = {
      checking: "checking…",
      ok: "working",
      unconfigured: "no API key configured",
      quota_exceeded: "out of credits/quota",
      rate_limited: "rate-limited right now",
      auth_failed: "API key rejected",
      unavailable: "network error / unreachable",
      unknown: "check failed for an unknown reason",
    };
    return labels[status] ?? status;
  }

  return (
    <main>
      <h1>AgentOps Wiki</h1>
      <p className="subtitle">
        Pick a local directory of <code>.md</code> notes (Obsidian vault,
        <code>~/dev/mywiki</code>, any layout). Search and ask questions
        grounded in your own knowledge base.
      </p>

      <div className="toolbar">
        {authMode?.mode === "dev" ? (
          <span className="status">
            ✓ dev-mode auto-mint active — signed in as
            <strong> {authMode.principal}</strong>
            (token expires in {Math.round(authMode.expiresIn / 60)} min)
          </span>
        ) : (
          <input
            type="password"
            placeholder="Bearer token (JWT)"
            value={bearer}
            onChange={(e) => setBearer(e.target.value)}
            suppressHydrationWarning
          />
        )}
      </div>

      <section className="card">
        <h2>1. Pick your wiki directory</h2>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button onClick={onPickDirectory} disabled={busy || !bearer} className="primary">
            {busy ? "..." : "Pick directory"}
          </button>
          <label className="muted" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            Retrieval:
            <select
              value={retrieval}
              onChange={(e) => setRetrieval(e.target.value as RetrievalMode)}
              disabled={busy}
              title="Applies when you pick a directory -- see README's Retrieval algorithm section"
            >
              <option value="tfidf">TF-IDF (cosine)</option>
              <option value="bm25">BM25</option>
              <option value="dense">Dense (bge-small, ONNX)</option>
              <option value="hybrid">Hybrid: BM25 + dense (RRF)</option>
              <option value="hybrid_rerank">Hybrid + cross-encoder rerank</option>
            </select>
          </label>
          {retrieval !== "tfidf" && retrieval !== "bm25" && (
            <span className="muted" style={{ fontSize: "0.85em" }}>
              first pick with this mode downloads ~150MB of model weights and
              takes ~10–30s — this is expected, not a hang.
            </span>
          )}
        </div>
        {corpusId && (
          <p className="status">
            ✓ indexed <strong>{docCount}</strong> file
            {docCount === 1 ? "" : "s"} in <strong>{indexDurationMs}ms</strong>
            {" "}(retrieval: <strong>{resolvedRetrieval}</strong>)
            {isObsidianVault && (
              <span className="muted"> · Obsidian vault detected — references open in Obsidian</span>
            )}
            <br />
            <span className="muted">corpus_id = {corpusId}</span>
          </p>
        )}
      </section>

      {corpusId && (
        <>
          <section className="card">
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <h2 style={{ margin: 0 }}>2. Chat (multi-turn)</h2>
              <label className="muted" style={{ display: "flex", gap: 6, alignItems: "center" }}>
                Provider:
                <select
                  value={provider ?? ""}
                  onChange={(e) => setProvider(e.target.value as Provider)}
                  disabled={busy || !provider}
                  title="Which server-side LLM key answers the next question. Keys are configured in .env, never sent from the browser."
                >
                  {availableProviders.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </label>
              {availableProviders.length === 1 && (
                <span className="muted" style={{ fontSize: "0.85em" }}>
                  only local-fake is available — set AGENTOPS_MINIMAX_API_KEY or
                  AGENTOPS_OPENAI_API_KEY in .env to unlock a real provider.
                </span>
              )}
              {availableProviders
                .filter((p) => p !== "local-fake")
                .map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => checkProviderStatus(p)}
                    disabled={providerStatus[p] === "checking"}
                    title={`${p}: ${providerStatusLabel(providerStatus[p])} — click to make one real test call (small cost)`}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                      fontSize: "0.85em",
                      background: "none",
                      border: "1px solid var(--border)",
                      borderRadius: 999,
                      padding: "2px 10px",
                    }}
                  >
                    <span
                      aria-hidden="true"
                      style={{
                        display: "inline-block",
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        background: providerStatusColor(providerStatus[p]),
                      }}
                    />
                    {p}
                  </button>
                ))}
            </div>
            <MetricsGuide topic="qa" />
            <p className="muted" style={{ marginTop: -4 }}>
              Each follow-up question uses the prior turn's context
              (the server keeps the conversation via a LangGraph
              checkpointer; the client only ever sends a single
              opaque <code>thread_id</code>). Picking a new directory
              starts a fresh conversation.
            </p>

            <div className="chat-thread">
              {messages.length === 0 && (
                <p className="status">
                  Ask a question below — top-{topK} evidence is
                  retrieved fresh each turn, and the assistant
                  remembers what you've already asked.
                </p>
              )}
              {messages.map((m, i) =>
                m.role === "user" ? (
                  <div className="chat-turn user" key={i}>
                    <div className="chat-bubble">{m.content}</div>
                  </div>
                ) : (
                  <div className="chat-turn assistant" key={i}>
                    <p className="muted" style={{ marginBottom: 4, fontSize: "0.8em" }}>
                      retrieval: <strong>{m.retrieval}</strong> · provider: <strong>{m.provider}</strong>
                    </p>
                    <p className="overall" style={{ marginBottom: 8 }}>
                      <strong>ROUGE-L F1</strong>
                      <span className="badge" style={{ backgroundColor: groundednessColor(m.overall_rouge_l_f1) }}>
                        {(m.overall_rouge_l_f1 * 100).toFixed(0)}%
                      </span>
                      <strong style={{ marginLeft: 16 }}>Citation Recall</strong>
                      <span
                        className="badge"
                        style={{ backgroundColor: groundednessColor(m.citation_recall) }}
                        title="Fraction of the LLM's sentences that carry a [N] citation resolving to real evidence"
                      >
                        {(m.citation_recall * 100).toFixed(0)}%
                      </span>
                      <strong style={{ marginLeft: 16 }}>Citation Precision</strong>
                      <span
                        className="badge"
                        style={{ backgroundColor: groundednessColor(m.citation_precision) }}
                        title="Fraction of emitted [N] markers that resolve to real evidence (catches fabricated citations)"
                      >
                        {(m.citation_precision * 100).toFixed(0)}%
                      </span>
                      <strong style={{ marginLeft: 16 }}>Faithfulness</strong>
                      <span
                        className="badge"
                        style={{ backgroundColor: groundednessColor(m.faithfulness) }}
                        title="Maynez et al., 2020: fraction of answer sentences whose tokens appear in the cited evidence. Independent of citations: catches unsupported claims that happen to cite a real [N] but paraphrase inaccurately."
                      >
                        {(m.faithfulness * 100).toFixed(0)}%
                      </span>
                    </p>
                    <div className="chat-bubble answer">{m.content}</div>

                    {m.sentences.length > 0 && (
                      <details
                        className="chat-detail"
                        open={expandedTurns.has(i)}
                        onToggle={(e) => {
                          const open = (e.target as HTMLDetailsElement).open;
                          setExpandedTurns((prev) => {
                            const next = new Set(prev);
                            if (open) next.add(i);
                            else next.delete(i);
                            return next;
                          });
                        }}
                      >
                        <summary>Per-sentence grounding ({m.sentences.length})</summary>
                        {m.sentences.map((s, j) => (
                          <div
                            key={j}
                            className="sentence"
                            style={{ borderLeft: `4px solid ${groundednessColor(s.rouge_l_f1)}` }}
                          >
                            <div className="sentence-header">
                              <span className="badge" style={{ backgroundColor: groundednessColor(s.rouge_l_f1) }}>
                                ROUGE-L F1 {(s.rouge_l_f1 * 100).toFixed(0)}%
                              </span>
                              <span className="muted" title="Precision = LCS ÷ sentence tokens. Recall = LCS ÷ evidence tokens.">
                                P={(s.rouge_l_precision * 100).toFixed(0)}% · R={(s.rouge_l_recall * 100).toFixed(0)}%
                              </span>
                              <span className="muted" title="Longest Common Subsequence length, out of the sentence's own token count.">
                                LCS={s.lcs_length}/{s.sentence_tokens} tokens
                              </span>
                              {s.cited_refs.length > 0 && (
                                <span className="muted">
                                  cites: {s.cited_refs.map((r) => `[${r}]`).join(" ")}
                                </span>
                              )}
                              {s.unresolved_refs.length > 0 && (
                                <span className="muted" style={{ color: "#dc2626" }}>
                                  unresolved: {s.unresolved_refs.map((r) => `[${r}]`).join(" ")}
                                </span>
                              )}
                            </div>
                            <div>{s.sentence}</div>
                          </div>
                        ))}
                      </details>
                    )}

                    {m.hits.length > 0 && (
                      <details
                        className="chat-detail references-tab"
                        open
                      >
                        <summary>References ({m.hits.length})</summary>
                        {m.hits.slice(0, 5).map((h, j) => (
                          <article className="hit" key={`chat-${i}-${h.ref_id}-${j}`}>
                            <div className="hit-title">
                              {h.obsidian_uri ? (
                                // obsidian:// is a custom protocol handled
                                // by the desktop app. The browser needs to
                                // navigate the *current* tab for the OS
                                // protocol handler to take over -- opening
                                // a new tab (target="_blank") is silently
                                // rejected because most browser installations
                                // do not register `obsidian` as a new-tab
                                // scheme. So: a plain anchor, no target,
                                // and a `pointerdown` fallback that uses
                                // window.location.assign (which respects
                                // the protocol handler the same way a user
                                // typing the URL into the address bar would).
                                <a
                                  className="hit-link"
                                  href={h.obsidian_uri}
                                  title={`Open in Obsidian vault — ${h.source_path}`}
                                  onClick={(e) => {
                                    e.preventDefault();
                                    window.location.href = h.obsidian_uri!;
                                  }}
                                >
                                  <code>{h.source_path}</code> — <em>{h.ref_id}</em>
                                </a>
                              ) : (
                                <a
                                  className="hit-link"
                                  href={`file:///${h.source_path}`}
                                  target="_blank"
                                  rel="noreferrer"
                                  title={`Open ${h.source_path}`}
                                >
                                  <code>{h.source_path}</code> — <em>{h.ref_id}</em>
                                </a>
                              )}
                            </div>
                            <div className="hit-meta">
                              <span className="metric">
                                score <strong>{h.score.toFixed(3)}</strong>
                              </span>
                              <span className="metric-sep">·</span>
                              <span
                                className="metric"
                                style={{ color: groundednessColor(h.coverage) }}
                              >
                                coverage <strong>{(h.coverage * 100).toFixed(0)}%</strong>
                              </span>
                              <span className="metric-sep">·</span>
                              <span className="metric muted">
                                mtime {new Date(h.mtime).toISOString().slice(0, 19)}
                              </span>
                            </div>
                          </article>
                        ))}
                      </details>
                    )}
                  </div>
                ),
              )}
              {busy && messages.length > 0 && messages[messages.length - 1].role === "user" && (
                <div className="chat-turn assistant" key="thinking">
                  <div className="chat-bubble chat-thinking">
                    <span className="chat-spinner" aria-hidden="true" />
                    Thinking{provider && provider !== "local-fake" ? ` (${provider})` : ""}
                    {retrieval !== "tfidf" && retrieval !== "bm25" ? " — retrieving with " + retrieval + "…" : "…"}
                  </div>
                </div>
              )}
            </div>

            <form onSubmit={onSend} className="search-row">
              <input
                placeholder="ask a follow-up (the assistant remembers the prior turn)"
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
              />
              <input
                type="number"
                min={1}
                max={20}
                value={topK}
                onChange={(e) => setTopK(Math.max(1, Math.min(20, Number(e.target.value) || 5)))}
                style={{ flex: "0 0 72px" }}
                title="top_k: how many documents to retrieve per turn"
              />
              {busy ? (
                <button type="button" onClick={onCancelSend}>
                  Cancel
                </button>
              ) : (
                <button type="submit" className="primary" disabled={!chatInput.trim()}>
                  Send
                </button>
              )}
            </form>
          </section>
        </>
      )}

      <MetricsPanel bearer={bearer} retrieval={corpusId ? resolvedRetrieval : null} />

      {error && (
        <p className="status" style={{ color: "var(--accent)" }}>
          error: {error}
        </p>
      )}
    </main>
  );
}
