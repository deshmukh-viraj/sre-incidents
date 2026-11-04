# SRE-MA — Multi-Agent Incident Response (Fintech SRE)

A LangGraph-based multi-agent incident-response system: an Alertmanager webhook
triggers a bounded, evidence-driven investigation across Prometheus/Loki/Tempo;
the agent forms grounded hypotheses, proposes typed remediations, executes
reversible actions inside a policy allowlist (max 2 per incident), verifies
recovery against live metrics, and classifies the outcome with the frozen
Five-Gate Attribution Framework — handing off to a human with a full evidence
package whenever evidence runs out.

```
alert ─► detector ─► diagnoser ─┬─► remediator ─► policy ─┬─► execute ─► verify ─► communicate
   │                            ├─► deep investigation ─► critic ┤                  └─► escalate
   │                            └─► escalate ◄──────── human approval gate (interrupt)
   └──► communicator (parallel)
```

Full details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/AGENT_ROSTER.md](docs/AGENT_ROSTER.md) ·
[docs/TOOL_CATALOG.md](docs/TOOL_CATALOG.md) ·
[docs/ATTRIBUTION_ADDENDUM.md](docs/ATTRIBUTION_ADDENDUM.md)

## Quickstart

```bash
# 1. environment
cd SRE-MA
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. configuration (no secrets in the repo — set keys in .env)
cat > .env <<'EOF'
OPENROUTER_API_KEY=...        # or GROQ_API_KEY / NVIDIA key per src/agents/utils.py
LANGFUSE_PUBLIC_KEY=...       # optional; enables LLM tracing
LANGFUSE_SECRET_KEY=...
EOF

# 3. simulator stack (default environment pack: fintech_sim)
cd ../fintech-sre-sim && docker compose up -d && cd ../SRE-MA

# 4. agent API
export DRY_RUN=true                       # writes are recorded no-ops
uvicorn src.api.app:app --port 8080

# 5. send an alert (labels only — exactly what Alertmanager posts)
curl -X POST localhost:8080/webhook/alert -H 'Content-Type: application/json' -d '{
  "status": "firing",
  "alerts": [{"status": "firing", "labels": {
    "alertname": "PaymentGatewayP99LatencyHigh", "severity": "critical",
    "service": "payment_gateway", "runbook": "RB-001", "team": "payments"}}]
}'

# 6. watch it work
curl localhost:8080/incidents/<incident_id>       # live agent_state embedded
```

Human approval: `POST /incidents/{id}/approve` with `{"approved": true,
"approver": "you"}` resumes the paused graph (LangGraph interrupt on the
human_gate node).

## Tests

```bash
python -m pytest tests/ -q     # 50 tests: routing, policy bounds, frozen gates, adapters, evaluator
```

## Evaluation

`python src/evaluation/evaluator.py` runs the 7-scenario matrix against a live
API + simulator; reports land in `reports/eval_results/`. Methodology, honest
status and known limitations: [docs/EVAL_REPORT.md](docs/EVAL_REPORT.md).
Sandbox (EC2 OTel demo + tunnels) setup: [docs/SANDBOX_RUNBOOK.md](docs/SANDBOX_RUNBOOK.md).

## Evidenced system claims

- **Investigation tools are read-only by construction** — the backend adapter
  layer (`src/tools/backends/`) issues GETs only; write-side actions are
  DRY_RUN-gated and policy-gated (`tests/test_policy.py`).
- **Autonomous execution is bounded** — max 2 reversible pod/service-level
  actions per incident from the allowlist, confidence ≥ 0.50, mandatory
  tool-result citations; everything else interrupts for human approval.
- **Attribution is frozen and tested** — the Five-Gate framework's current
  behavior is pinned by `tests/test_attribution_gates.py`; thresholds were not
  weakened in this iteration.
- **Backends are swappable by config** — `config/environments.yaml` packs
  select endpoints/bounds; nodes and prompts contain no query dialects
  (`tests/test_backends.py`).
- **The evaluator cannot silently pass** — it reads the real scenario schema
  and is covered by drift-guard tests (`tests/test_evaluator.py`).

Not yet evidenced on this machine (requires the Docker sim stack + LLM keys):
live end-to-end scenario runs, Langfuse trace links, measured false-credit
rates. See docs/EVAL_REPORT.md.

## Repository layout

```
SRE-MA/
├── src/graph/        orchestrator, routing, policy, state, attribution, clear_time
├── src/agents/       detector, diagnoser, deep_investigation, critic, remediator,
│                     execution (human_gate + execute), communicator, escalation
├── src/tools/        sre_tool, agent_tool, kg_tool + backends/ (adapter layer)
├── src/api/          FastAPI app (webhook, approvals, incidents, status)
├── src/evaluation/   evaluator.py + data/scenarios.json
├── runbooks/         RB-001..RB-006 + runbooks.json action plans
├── config/           environments.yaml (environment packs)
├── tests/            pytest suite (no live services required)
└── docs/             architecture, roster, catalog, runbook, eval, decisions
fintech-sre-sim/      the simulation environment (read-only for the agent)
```
