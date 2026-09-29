"""LocalFakeAdapter — deterministic scripted responses for CI / unit tests.

When `provider=local-fake`, this adapter synthesizes an answer by
re-running the same retrieval the real graph uses, then composing a
"looks-like-a-real-LLM" response from the retrieved docs. Output
structure mirrors what a real model would produce (introduction + quoted
passage + follow-on detail + source attribution), so the Streamlit
debug surface looks like an actual agent ran — not a string-lookup.

For CI / held-out benchmarks where exact text matters, the script
directory `fixtures/llm/scripts/` still holds the canned responses
and the `_pick_scripted_response` path remains available (call with
`use_synthesis=False`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .adapter import ChatResult, LLMAdapter, Usage


def _strip_yaml_frontmatter(text: str) -> str:
    """Drop an Obsidian-style `--- \\nkey: value\\n...\\n---` frontmatter
    block from the start of `text`.

    The wiki corpus stores Obsidian frontmatter at the top of every note.
    Quoting that metadata back in the synthesized answer reads like a
    debug dump, not a real LLM answer, so the local-fake synthesizer
    strips it before slicing a representative passage. Real providers
    would do this implicitly -- this is the canned equivalent.
    """
    if not text.startswith("---"):
        return text
    # End of the frontmatter is the next `---` line. If there is none
    # (malformed file), return the body unchanged rather than eating
    # the whole document.
    end = text.find("\n---", 3)
    if end == -1:
        return text
    # Skip the closing fence plus the newline that follows it.
    rest = text[end + 4 :]
    return rest.lstrip("\n")


class LocalFakeAdapter(LLMAdapter):
    provider = "local-fake"
    model = "local-fake-v1"

    def __init__(self, scripts_dir: Path | None = None) -> None:
        self._scripts_dir = scripts_dir or (
            Path(__file__).resolve().parent.parent.parent.parent
            / "fixtures"
            / "llm"
            / "scripts"
        )
        self._counter = 0
        self._script: list[dict[str, str]] = []
        self._load_script("default.jsonl")
        self._synth_counter = 0

    def _load_script(self, name: str) -> None:
        path = self._scripts_dir / name
        if not path.exists():
            self._script = [
                {"role": "assistant", "content": "local-fake default response"}
            ]
            return
        self._script = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if not self._script:
            self._script = [
                {"role": "assistant", "content": "local-fake default response"}
            ]

    @staticmethod
    def _extract_retrieval_block(messages: list[dict[str, str]]) -> str:
        """Pull the evidence block out of the user prompt.

        Two graph variants ship today:

        - `wiki_chat.py` (the live web-chat path) uses
              "Question: {q}\\n\\n## Evidence\\n\\n{body}\\n\\n## Answer"
        - `fixed.py` / `planner_executor.py` (legacy test/dev paths)
          use "Retrieved docs:\\n{body}\\n\\nTask: {task}"

        We parse whichever is present, preferring the live format. The
        legacy block shape is identical (numbered footnote header per
        chunk), so `_parse_doc_blocks` doesn't care which it got.
        """
        joined = "\n".join(m.get("content", "") for m in messages)
        new_match = re.search(
            r"## Evidence\s*\n\n(.*?)\n\n## Answer",
            joined,
            flags=re.DOTALL,
        )
        if new_match:
            return new_match.group(1)
        legacy_match = re.search(
            r"Retrieved docs:\n(.*?)\n\nTask:",
            joined,
            flags=re.DOTALL,
        )
        return legacy_match.group(1) if legacy_match else ""

    @staticmethod
    def _extract_task(messages: list[dict[str, str]]) -> str:
        joined = "\n".join(m.get("content", "") for m in messages)
        # Live wiki_chat prompt: "Question: {q}\n\n## Evidence\n\n...\n\n## Answer"
        m = re.search(r"^Question:\s*(.+?)\n\n## Evidence", joined, flags=re.DOTALL | re.MULTILINE)
        if m:
            return m.group(1).strip()
        # Legacy fixed.py / planner_executor prompt
        m = re.search(r"\n\nTask:\s*(.+?)\s*$", joined, flags=re.DOTALL)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _parse_doc_blocks(retrieval: str) -> list[tuple[str, str]]:
        """Split "Retrieved docs:\\n[N] (source: ...)\\n{body}\\n\\n--\\n\\n[N] (source: ...)\\n{body}"
        into (ref_num, body) tuples.

        Mirrors the format produced by `wiki_chat.footnote_evidence_blob`:
        numbered footnote label on the first line, then the evidence
        snippet, joined by `\\n\\n--\\n\\n`. The legacy `[stem]: snippet`
        format (older `graph/fixed.py`) is no longer produced by the
        wiki_chat graph but is still parsed defensively so a script that
        hand-rolls the old format keeps working.
        """
        if not retrieval or retrieval == "(no relevant docs found)":
            return []
        blocks: list[tuple[str, str]] = []
        for chunk in retrieval.split("\n\n--\n\n"):
            # New (wiki_chat) format: first line is "[N] (source: ..., score: ...)"
            new_match = re.match(
                r"\[(\d+)\]\s*\(source:[^)]*\)\s*\n(.*)$",
                chunk,
                flags=re.DOTALL,
            )
            if new_match:
                body = _strip_yaml_frontmatter(new_match.group(2)).strip()
                blocks.append((new_match.group(1), body))
                continue
            # Legacy ([stem]: snippet) format -- still parsed so old
            # fixtures and ad-hoc scripts keep working.
            head, _, body = chunk.partition("]: ")
            if body:
                body = _strip_yaml_frontmatter(body).strip()
                blocks.append((head.lstrip("[").rstrip(), body))
        return blocks

    @staticmethod
    def _synthesize(task: str, docs: list[tuple[str, str]]) -> str:
        """Compose a realistic-looking answer from the retrieved docs.

        The answer carries inline `[N]` markers next to every claim so
        the groundedness scorer picks up citations (the wiki_chat graph
        scores the *raw* answer -- before the deterministic References
        footer is appended -- so a markerless answer yields 0 for every
        metric, which is misleading even when the docs were retrieved).
        """
        if not docs:
            return (
                "I searched the corpus and found no direct match. "
                "Could you provide the project name or specific error message?"
            )

        # Pull a representative passage from the lead doc. Collapse
        # whitespace so a `[N]` marker can sit cleanly on the line.
        lead_ref, lead_body = docs[0]
        lead_text = re.sub(r"\s+", " ", lead_body).strip()
        passage = lead_text[:200].rsplit(".", 1)[0] + "." if "." in lead_text[:200] else lead_text[:200]

        intro = (
            "Based on the corpus, here's what I found for your question:\n\n"
            f"{passage} [{lead_ref}]\n"
        )
        followon_lines: list[str] = []
        for ref, body in docs[1:]:
            extra_text = re.sub(r"\s+", " ", body).strip()
            extra_passage = (
                extra_text[:160].rsplit(".", 1)[0] + "."
                if "." in extra_text[:160]
                else extra_text[:160]
            )
            followon_lines.append(f"{extra_passage} [{ref}]")
        if followon_lines:
            followon_lines.append(
                f"\nSupporting sources: {', '.join(r for r, _ in docs[1:])}."
            )
        followon_lines.append(
            f"\nSource: [{lead_ref}] (retrieved from the wiki corpus). "
            "Real providers quote verbatim; local-fake synthesizes a "
            "shorter summary so the response stays under token limits."
        )
        followon = "\n".join(followon_lines)

        return intro + followon

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kw: Any,
    ) -> ChatResult:
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        prompt_tokens = max(1, prompt_chars // 4)

        # Synthesize from the retrieved docs so the response looks like
        # a real model reading the corpus, not a canned line.
        retrieval = self._extract_retrieval_block(messages)
        docs = self._parse_doc_blocks(retrieval)
        task = self._extract_task(messages)
        content = self._synthesize(task, docs)

        completion_tokens = max(1, len(content) // 4)

        usage = Usage(
            provider=self.provider,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=0.0,
        )
        self._last_usage = usage
        return ChatResult(content=content, usage=usage)
