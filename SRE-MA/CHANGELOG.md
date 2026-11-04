# Changelog

All notable changes to SRE-MA. Dates are 2026-11-03.

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
