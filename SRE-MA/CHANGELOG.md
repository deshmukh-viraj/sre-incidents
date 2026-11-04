# Changelog

All notable changes to SRE-MA. Dates are 2026-11-03 (v2 integration) and 2026-11-04 (live validation).

## [2026-11-04] — v2-refactor integration + live otel-demo validation (feature/v2-refactor)

### Live validation (AWS otel-demo, real remediation)
- End-to-end honest loop proven on the opentelemetry-demo stack: cartFailure
  feature flag forced to the failure variant (`100%`), alert
  `OTelUserFacingErrorRateHigh` fired with **no answer in it** (labels and
  annotations neutralized), the agent investigated with its own bounded
  ReAct sweep and **named the flag itself** from its evidence
  (`feature_flag_evaluation_success_total{service_name="cart",
  feature_flag_key="cartFailure"}` → `cartFailure → off`), executed through
  the admin bridge on the instance, and the alert cleared — only because the
  fix really worked. Result: `AGENT_REMEDIATED`, all five gates True,
  MTTR business 231 s / agent 80 s, ~58 k tokens (~$0.03) per run.
- Answer-removal: `remote_flag` / `remote_variant` labels stripped from the
  alert; `propose_remediation(flag, variant)` + writeback → state → plan
  params chain added so the executor reads the agent's proposal, not labels.
- HITL proven both ways: an unverifiable-target proposal (KG down) routes to
  human approval instead of a wrong rejection; approve/reject via
  `/incidents/{id}/approve` resumes the graph through `Command(resume=...)`.

### Added
- `aws-admin/admin_server.py` — remote-flag bridge: validates flag/variant
  against the demo's own schema over SSH, flips `demo.flagd.json`, reloads
  flagd; unknown flags rejected. Backs the `set_feature_flag` tool.
- `src/tools/log_tool.py` — OpenSearch logs backend (v2's `LOG_BACKEND=opensearch`
  branch referenced a module that was never pushed; reconstructed).
- `src/graph/budget.py` — v2's state-carried budget (P9 contract from
  `tests/test_budget_fallback.py`), also missing at v2 HEAD.
- Static topology fallback in `kg_tool.get_dependencies` (fintech + otel-demo
  maps, same data `kg_ingest` seeds) so target binding and the critic's
  dependency check work when Neo4j is unreachable.
- Gate-2 binding is dependency-aware: an action on a declared dependency of
  the alerted service binds to the affected system, recorded with
  `target_bound_via` for the audit trail (gate math unchanged; an unrelated
  service still fails to bind).
- NVIDIA provider support: provider-tolerant `structured_invoke` in
  `src/agents/utils.py` (ChatNVIDIA lacks `include_raw=True`), used by
  communicator / diagnoser / verifier.
- Golden tests regenerate over the agent-proposed flag/variant (9c2dd9e).

### Fixed
- v2 HEAD was not runnable as pushed: 3 committed conflict blocks in
  `execution_agent.py` (syntax error), imports of two never-committed
  modules, evaluator KeyError-ing every scenario, 6 silently-dropped service
  aliases in `fintech.yaml` (misrouting), `requirements.txt` concatenation.
- Remote flag verification: propagation grace 75 s (not the full alert-dilution
  sleep — it inflated the gate-3 lead to the sleep duration) + gate-3
  tolerance for remote flips = `REMOTE_FLAG_EFFECT_SECONDS` (300 s) +
  overhead, covering the demo's trailing 5 m window dilution.
- `check_alert_status` resolved-vs-unknown semantics.
- 168 tests green (`pytest tests/ -q`).

## [unreleased] — hardening iteration (feature/deep-agent-refactor)

### Added
- Backend adapter layer `src/tools/backends/`: `MetricsBackend` /
  `LogsBackend` / `TracesBackend` contracts, prometheus / loki / tempo
  adapters, environment packs in `config/environments.yaml`
  (`SRE_ENVIRONMENT`, default `fintech_sim`; `sandbox` adds Tempo).
  All query-dialect authoring now lives in the adapters.
- Spec action bounds in the policy gate: max 2 actions per incident (extras
  rejected + recorded), cluster-level or not-known-reversible actions routed to
  human approval, reversibility derived from a tool map (`REVERSIBLE_WRITE_TOOLS`)
  instead of planner stamps.
- Tool-result citations on action plans; uncited deep-agent proposals route to
  human approval instead of auto-execution.
- Human-handoff evidence package: escalation messages now carry recorded tool
  results + diagnosis issues and cite the max-confidence hypothesis.
- Per-investigation duplicate-read cache (identical queries served from the
  record with a `[DUPLICATE]` marker) and a stagnation gate on the canned-fix
  fallback (`>= 2` distinct fresh queries, else escalate).
- Tempo traces backend; `/agents/status` reports traces health.
- Runnable pytest suite (50 tests): routing, policy bounds, frozen five-gate
  matrix, backend adapters, evaluator schema guards; live-API webhook smoke
  (skips without a server). `conftest.py`, pytest in requirements.

### Fixed
- Evaluator schema drift: reads `ground_truth.*` / `evaluation_criteria.*` and
  nested `agent_state` (previously KeyError'd every scenario into a 0.0);
  attribution_match only scored when a scenario declares an expectation;
  executed_unverified treated as terminal; eval payloads no longer carry
  `__sim_scenario` ground-truth keys.
- `ATTRIBUTION_HOLDOUT_RATE` import crash when unset (defaults to 0.0).
- LLM base_url env read (`base_url` → `OPENAI_BASE_URL`); stray
  `escalation_reason` key in initial_state.
- Critic no longer rejects unknown runbook IDs: `KNOWN_RB` derives from
  `runbooks.json`.
- `tests/test_graph.py` executed an HTTP POST at import time, breaking pytest
  collection.

### Changed
- `prometheus_tool` / `loki_tool` / `sre_tool` are capability facades over the
  backend layer (signatures preserved); `execution_agent` verification
  expressions and the API health check come from adapter templates;
  `clear_time.query_range` defaults to the active pack's endpoint/timeout.
- Investigator prompt no longer teaches PromQL; it points at runtime-returned
  queries (`query_topology` / `list_metrics`).

### Docs
- New: `docs/ARCHITECTURE.md`, `docs/AGENT_ROSTER.md`, `docs/TOOL_CATALOG.md`,
  `docs/SANDBOX_RUNBOOK.md`, `docs/EVAL_REPORT.md`, `docs/DECISION_LOG.md`,
  `docs/ATTRIBUTION_ADDENDUM.md`, `docs/issues/ISSUES.md`; README rewritten
  (real quickstart + evidenced claims).

### Unchanged (frozen)
- Five-Gate Attribution Framework thresholds and behavior (pinned by tests).
- Deterministic spine semantics: detector, fast-path diagnoser, critic,
  policy, executor.
- Simulator (`fintech-sre-sim`) files: untouched.
