# Verified run crashed by telemetry calls during/after gate-5 polling

**Date:** 2026-10-28
**Status:** Resolved

## Problem
In the sandbox E2E (webhook → agent → DRY-RUN execution → verification), runs that reached the executor crashed *after* successful remediation with status `failed`. Three distinct crash sites, same symptom:

- `Remote end closed connection without response` inside `clear_time.query_range` during gate-5 stability polling (`execute_node`) — one transient transport blip killed an otherwise verified run (Business MTTR computed, `verified=True`, then crash).
- `Remote end closed connection without response` from the Langfuse SDK keep-alive dying during the ~90s verify sleep, surfacing through the graph callback tail.
- `1 validation error for Projects data Field required` from `client.get_trace_url()` when the Langfuse host was unreachable/at the end of a completed run.

Reproduce: run the API server, fire the RB-001-style Alertmanager webhook, wait for the execute/verify cycle.

## Root cause
Telemetry and evidence calls were unguarded: a single transient network error after a completed remediation propagated out of `sre_graph.invoke` and flipped the incident to `failed`. Also, `human_gate_node` sent the approval Slack page *before* `interrupt()`; on `Command(resume=...)` the node re-runs from the top and the interrupted run's partials are discarded, so the page fired twice.

## Solution
- `src/graph/clear_time.py` `query_range`: one retry on `OSError` (covers `URLError`/`RemoteDisconnected`); real errors still raise after the retry. Commit `f107dee`.
- `src/graph/orchestrator.py` `run_incident`: `get_trace_url()` wrapped in try/except — trace URLs are best-effort. Commit `d5886d0`.
- `src/agents/execution_agent.py` `human_gate_node`: page *after* the decision arrives, once. Commit `712c73f`.

## Verification
`./mango/Scripts/python.exe -m pytest tests -q --ignore=tests/test_graph.py` → 119 passed, 1 pre-existing failure (`test_chatopenai_config`, baseline). The E2E progressed past all three sites: gate-5 polling completed, RecoveryChecker verdict `recovered`, Langfuse scores logged (`status: escalated, class: escalated_with_evidence` on the monitoring-outage run).

## Notes
The Langfuse-keep-alive crash was only fully neutralized in the sandbox by pointing `LANGFUSE_BASE_URL` at a local 200-OK sink (`SRE-MA/tmp_e2e/fake_langfuse.py`) and MLflow at a `file://` store; in a healthy deployment the guard changes make single blips non-fatal either way.
