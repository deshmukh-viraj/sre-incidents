# Roster Implementation Record (P0–P10)

**Date:** 2026-10-28
**Branch:** `feat/roster-upgrades` (from `8b25265`)
**Scope:** worker-roster upgrades per the 2026-10-28 prompt. Every phase is flag-gated (default 0; `MULTI_AGENT=1` enables all). The deterministic spine is frozen by `tests/test_golden_control_scenarios.py` (golden JSON, flags off — RB-001 byte-identical).

## Phase map

| Phase | Commit | Deliverable | Flag (default 0) |
|---|---|---|---|
| P0 | `5b3d00f` | Tool Librarian: `src/tools/registry.py` (ToolSpec catalogue from agent_tool) + `src/tools/dispatcher.py` (`catalog_search` / `explain_tool` / `run_tool` — single dispatch site, one timeout, row caps, read-only enforcement, never raises) | always on, additive |
| P1 | `0b5735e` | Schemas single-source (`src/agents/schemas.py`; utils re-exports), roster state fields + reducers (`_list_add`, `_add_int`, `_merge_budget`), golden-file control test | — |
| P2 | `9fbf874` | B1 Verifier node: 2 structured challenges, ≤3 disproof queries via dispatcher, cap-1 reroute, fail-open to critic; R7 dunder-key prompt hardening | `VERIFIER_ENABLED` |
| P3 | `52f2e65` | B2 RecoveryChecker: `src/graph/recovery_checker.py`, verdict from the single verification poller (`recovered` / `not_recovered` / `flap_risk`); execute emits `checker_result` | `RECOVERY_CHECKER_ENABLED` |
| P4 | `af6b20d` | B4 typed HypothesisReport emission in llm_diagnoser (per-hypothesis fallback keeps old path on validation error) | `TYPED_DIAGNOSIS_ENABLED` |
| P5 | `ba30262` | B5 remediator bounded pre-flight mini-loop: ≤3 read-only validations, novel (`deep_agents`) path only; stale premise forces `requires_approval=True`, plan content unchanged | `REMEDIATOR_PREFLIGHT_ENABLED` |
| P6 | `fa55eb9` | B6 executor stub: typed `ExecutionReceipt`s built from results already in hand; DRY_RUN preserved, zero new egress | `EXECUTOR_RECEIPTS_ENABLED` |
| P7 | `a2e6eb9` + `712c73f` | B7 handoff package on budget/loop exhaustion (built once by escalate, transferred once by human_gate) + canonical HITL resume via `Command(resume=...)`; approval page now happens after the decision (replay-safe single notify) | `HANDOFF_ENABLED` |
| P8 | `d2a8ae0` | B8 communicator consumes receipts/handoff: agent-authored final report section + fallback path keeps the facts | `COMMS_ENABLED` |
| P9 | `b91f062` | TASK C wiring: LLM fallback chain (primary → other configured providers → deterministic except), global Budget middleware wrapping the 4 LLM workers, version/tool/receipt scores in Langfuse | `FALLBACK_ENABLED`, `BUDGET_ENABLED` |
| P10 | `59e28f3` | TASK D fintech config pack: `src/config/fintech.yaml` + loader (thresholds, service aliases, gateway metric names, runbooks path) | `CONFIG_PACK_ENABLED` |

Follow-up fixes from the live E2E: `f107dee` (`query_range` retries one transient transport blip), `d5886d0` (trace-url telemetry guarded), `712c73f` (single notify across interrupt+resume).

## R-rule proofs (grep, 2026-10-28)

- **R7** — no `__`-prefixed state keys reach prompts: both signal-string builders filter (`diagnoser_agent.py:185`, `deep_investigation.py:199`); Langfuse gets tags/scores only.
- **One dispatch site** — only `dispatcher.run_tool` executes tools; its two callers are read-only verification paths (verifier, remediator pre-flight).
- **One handoff-to-human path** — package built only in `escalation_agent.py` (`_handoff_package`), transfer only in `human_gate_node` (interrupt carries reason + options; decision maps to unchanged approval semantics).
- **One budget** — `budget_exhausted` is set only by `src/graph/budget.py`; routers only read it.
- **Golden spine** — 265-line golden JSON, flags off, unchanged through all phases.

## Prompt vs. repo conflicts (recorded, not chased)

| Prompt claim | Reality |
|---|---|
| `params.yaml`, `COST_THRESHOLD` | do not exist |
| `infer_service` at routing.py:463 | at routing.py:504 (now with `_DEFAULT_SERVICE_ALIASES` + pack override) |
| sim scenario keys have trailing spaces | keys are clean (sim commits "fix casing" / "remove extra whitespace") |
| sim `main.py` uses `Request` without import; calls missing `engine.is_effective_action` | refuted — no such code in `fintech-sre-sim/generators/main.py` or `scenario_engine.py` (see `docs/issues/2026-10-28-sim-boot-defect-claim-refuted.md`) |
| `test_chatopenai_config` fails on timeout/thinking asserts | pre-existing baseline failure (`utils._create_llm`); untouched per spec, 1 failed / 119 passed throughout |
| `grounded_evidence` state field | never existed; ledger uses `investigation_scratchpad` |
| HITL resume via `update_state` + `invoke(None)` | confirmed live (app.py:669/:318) and replaced by `Command(resume=...)` (P7) |

## Environment notes for evaluation

- Test/compile: `./mango/Scripts/python.exe -m pytest tests -q --ignore=tests/test_graph.py` from `SRE-MA/` (Windows venv via WSL interop; pass env vars with `WSLENV` — propagation is unreliable in this setup, prefer `cmd.exe /c "set VAR=...&& ..."`, which is how the E2E was driven).
- `tests/test_graph.py` needs the full server; excluded from the suite as before.
- Sandbox E2E ran against a fake Prometheus (`tmp_e2e/fake_prom.py`) + local Langfuse sink (`tmp_e2e/fake_langfuse.py`) + `file://` MLflow store. It exercised: webhook → signals → deep investigation (real LLM) → critic reject/repair ladder → verifier (contradicted reroute, cap 1, upheld, unverifiable) → policy → human_gate interrupt → **approve endpoint resuming via `Command(resume=...)`** → DRY-RUN execution → RecoveryChecker `recovered` (MTTR computed) → Langfuse scores logged.
- Live E2E with the real simulator: boot infra via `docker compose up -d` in `fintech-sre-sim/` (prometheus :9090 verified answering), sim app via its bundled venv (`apple/Scripts/python.exe -m generators.main --metrics-port 8000`, control API :8001, trigger via `/control/trigger/{scenario}`). Note: the sim-app Docker build fails on a `torch` CPU-wheel pip install; run the app from the venv instead. Left running for the user's evaluation.
- E2E-specific knobs used (do not affect flags-off behavior): `MULTI_AGENT=1`, `SIM_AUTO_APPROVE=true`, `DRY_RUN=true`, `PROMETHEUS_URL`, `LANGFUSE_BASE_URL` → local sink, `MLFLOW_TRACKING_URI` → `file://`, `-X utf8` for the cp1252 console.
