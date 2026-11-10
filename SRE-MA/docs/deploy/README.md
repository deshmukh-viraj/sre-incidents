# Deployment hardening templates (Phase 3 / security gaps 4-5)

These manifests are TEMPLATES: apply them with your platform team against your
real cluster — shapes assume Kubernetes and the agent's namespace is `sre-ma`.
They implement the two identity/egress controls from docs/TARGET_ARCHITECTURE.md
section 6 that code cannot enforce by itself:

- `sa-split.yaml` — read (investigation) and write (execution) run as separate
  service accounts. The policy validator decides; the cluster enforces. Even a
  fully-injected agent cannot exceed the write SA's RBAC.
- `network-policy.yaml` — deny-by-default egress: backends (Prometheus/Loki/
  Tempo), the LLM API, Alertmanager, and the agent's own API. Bounds the
  exfiltration leg of the lethal trifecta below the model.

## New environment variables (all optional; defaults preserve current behavior)

| Variable | Default | Purpose |
|---|---|---|
| `APPROVAL_TOKEN` | unset (open, sim regime) | When set, `/approve` and learning-approval require header `x-approver-token`. SET THIS IN ANY REAL ENVIRONMENT. |
| `APPROVAL_MAX_AGE_S` | 7200 | Approval window; stale decisions must go through manual resolve. |
| `SRE_AUTONOMY_DISABLED` | unset | Kill switch (`1` = every action human-gated, live-flippable). |
| `WEBHOOK_ALERT_RATE_PER_MIN` | 60 | Alert flood ceiling (cost-attack guard). |
| `MAX_ACTIVE_INCIDENTS` | 8 | Concurrent incident cap. |
| `AUDIT_CHAIN_PATH` | `src/data/audit_chain.jsonl` | Tamper-evident audit chain location (mount a volume in real deployments). |
| `LEARNING_QUEUE_PATH` | `src/data/learning_queue.jsonl` | Human-gated learning drafts queue. |

## Verification commands

```bash
# the audit chain must verify clean at any time
.venv/bin/python -c "from src.utils.audit import verify_chain; print(verify_chain())"

# the offline guard suite (cron this; exit 1 on regression)
.venv/bin/python -m src.evaluation.gym
```
