# Changelog

All notable changes to SRE-MA. Dates are 2026-11-03 (v2 integration), 2026-11-04 (live validation) and 2026-11-09 (post-mortem hardening).

## [2026-11-09] — post-mortem hardening: act-rule gates, evidence hygiene, tracing (feature/v2-refactor)

Fixes the seven failure modes from the cartFailure run post-mortem (GOAL_RECALL.md).
Golden regenerated for the one sanctioned label change (blast pod→service).

### Act-rule gates (FM1 — unverified SEV1 change ran with no approval)
- `policy_validator_node` enforces the goal's autonomy rule additively:
  - **Tier gate** — targets are looked up in a per-environment `service_catalog`
    (config/environments.yaml): `stateless_app` auto-eligible,
    `stateful_data`/`monitoring` always human gate, **missing from the catalog =
    human gate**. Catches e.g. RB-002's rolling restart on `account_ledger`,
    which previously auto-executed.
  - **Citation gate** — LLM (`deep_agents`) diagnoses must cite ≥2 evidence
    lines implicating the action target; the post-mortem's all-telemetry-empty
    investigation now human-gates instead of executing.
  - **Verifier gate** — a diagnosis the adversarial verifier did NOT uphold
    (unverifiable/ungrounded, or contradicted after the reroute cap) never
    auto-executes; it goes to the human gate with the plan prepared.
- `requires_human_approval` no longer blanket-gates every service-blast action;
  autonomy-eligible action types (flag flips, single-service restarts,
  informational ops) pass to the policy tier gate. `kubectl_scale`/`rollback`/
  patches still gate. Blast radius for feature-flag flips is honestly
  `service` (a global flag change was labeled `pod`), pinned line in
  test_routing.py updated with approval; RB-001 runbook label fixed likewise.
- Self-check: `check_act_gates.py` replays the post-mortem (empty telemetry +
  unverifiable verifier → human gate) and the healthy path (grounded + cited
  flag flip on a stateless tier → autonomous).

### Environment-correct tooling (FM2 — metrics tools pointed at the wrong system)
- `SERVICE_METRICS` gains curated PromQL for the otel-demo services
  (frontend/cart/checkout/payment/... on `service_name` labels), so
  `list_metrics`' static fallback and the KG's [Metrics] sections stop handing
  the agent fintech-sim queries (`card_rails`, `db="postgres_primary"`).
- Label gotchas split per environment (`label_notes_for(service)`): otel
  services see `service_name`/`http_response_status_code`/rpc conventions,
  fintech services see theirs. Tool error text no longer hardcodes fintech
  label examples.

### Answer hygiene (FM3)
- `propose_remediation` no longer leaks the answer: the docstring's
  "(usually 'off')" and the rejection message's `flag='cartFailure',
  variant='off'` example are gone — the LLM must name flag and variant from
  its own evidence.

### Reroute economics (FM4 — duplicate second investigation, ~75s + half the cost)
- The verifier reroutes only on a **grounded** non-upheld verdict; ungrounded
  results cannot be re-ground by re-running the same backends.
- A verifier reroute now inherits the per-run read cache (recorded results +
  duplicate suppression) and injects the VERIFIER CHALLENGE (verdict, reason,
  failed disproof queries) into the investigation context, so pass 2 adds new
  evidence instead of repeating queries.

### Cost (FM5)
- Verifier LLM calls run with thinking disabled (`simple=True`): a 38s,
  4,975-hidden-reasoning-token verdict call that returned a blank reason was
  cost without information. test_verifier fake signature extended (fakes only).

### Evidence hygiene (FM6)
- The finding is no longer written into its own evidence list (it lives in
  hypothesis/root_cause); evidence = observations only.
- Terminal receipts ("proposal recorded, investigation ended.") are excluded
  from `_tool_evidence`, the investigation scratchpad, and the critic's
  citations — they no longer count as tool-result grounding.
- Truncation caps raised (selector 120, result value 160) so evidence lines
  survive with their values.

### Tracing (FM7)
- `OTEL_SERVICE_NAME=sre-incident-agent` set in app.py before any langfuse
  import (traces no longer read `unknown_service:python`).
- Deep-investigation span named via `run_name` (was `ChatOpenAI`).
- Trace metadata now carries `tool_calls`/`tool_errors` counts so an
  all-tools-down run is visible in filters instead of reading as clean.


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
