# Runbook - AgentOps Workbench

## Bring up the stack

\`\`\`bash
cd ~/dev/agentops-workbench
uv sync --extra dev
cp .env.example .env
# edit .env: paste MINIMAX_API_KEY from /Users/sanghee/dev/dev-harness-kit/.env
AGENTOPS_PROVIDER=minimax uv run uvicorn agentops_workbench.api.server:app --host 127.0.0.1 --port 8000
\`\`\`

In a second terminal:

\`\`\`bash
cd ~/dev/agentops-workbench
AGENTOPS_PROVIDER=minimax uv run streamlit run streamlit_app/app.py
\`\`\`

## Clear the mock ledger

The mock ticket ledger writes to a process-local SQLite file. Drop it to
reset:

\`\`\`bash
rm -f /tmp/tickets-*.db
\`\`\`

## Read a trace

\`\`\`bash
# traces land under runs/<id>/trace.otel.jsonl after a run completes
cat runs/$(ls -t runs/ | head -1)/trace.otel.jsonl | head -20
\`\`\`

All credentials are redacted before the trace hits disk. If you spot a
cleartext credential, that is a bug - open an issue with the offending
span attributes.

## Re-run a held-out case

\`\`\`bash
cd ~/dev/agentops-workbench
AGENTOPS_PROVIDER=local-fake uv run python -c "
from agentops_workbench.benchmark.load import load_split
held = load_split('fixtures/cases/held_out', 'held_out')
for c in held:
    print(c.id, c.task)
"
\`\`\`

## Switch provider

\`\`\`bash
AGENTOPS_PROVIDER=minimax    # live; needs MINIMAX_API_KEY in .env
AGENTOPS_PROVIDER=local-fake # CI / unit tests; no key needed
AGENTOPS_PROVIDER=openai     # requires OPENAI_API_KEY
\`\`\`
