# Sim boot defect claim ("Request not imported / missing is_effective_action") refuted

**Date:** 2026-10-28
**Status:** Resolved (claim refuted — no defect found)

## Problem
The phase-0 pre-flight recorded a claim that `fintech-sre-sim` could not boot: `main.py` allegedly used `Request` without importing it and called a nonexistent `engine.is_effective_action`. This blocked the planned live E2E against the simulator.

## Root cause
None found. Grep over the actual entry points (`fintech-sre-sim/generators/main.py`, `generators/metrics_generator.py`, `generators/log_generator.py`, `scenarios/scenario_engine.py`, `scenarios/novel_catalog.py`) shows no bare `Request` usage and no `is_effective_action` reference anywhere. The entry point is `generators/main.py` (not a root `main.py`).

## Solution
No code change (sim is read-only per scope). Boot path verified instead:
- Infra: `docker compose up -d` in `fintech-sre-sim/` → prometheus/alertmanager/loki/promtail/grafana up; real Prometheus :9090 answering queries.
- Sim app: the Docker build fails on a `pip install torch --index-url=.../whl/cpu` step, so run it from the bundled venv: `apple/Scripts/python.exe -m generators.main --mode steady --metrics-port 8000` (scrape endpoint :8000/metrics, control API :8001, trigger via `POST /control/trigger/{scenario}`).

## Verification
`GET http://127.0.0.1:9090/api/v1/query?query=up` returned a live vector result scraping the sim targets; sim deps import cleanly from the `apple` venv.

## Notes
The stack was left running for the user's own simulator-driven evaluation.
