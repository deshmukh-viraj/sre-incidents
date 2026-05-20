# RB-001 — Payment Gateway Latency Spike

**Severity:** SEV-2 (→ SEV-1 if MTTR > 15 min)
**Service:** payment_gateway → card_rails (downstream)
**SLO Impact:** Latency SLO breach — p99 > 2 000 ms sustained (target: 500 ms)
**Regulation relevance:** PCI-DSS Req 6.4 (change management), Req 10.7 (audit log of failures)

---

## Alert Context

| Alert | Threshold | Firing condition |
|---|---|---|
| `PaymentGatewayP99LatencyHigh` | p99 > 1 500 ms for 3 min | 5-min burn rate |
| `SLOErrorBudgetBurnRateFast` | 14.4× budget burn | 1h window |
| `DownstreamDependencyDegraded` | card_rails error_rate > 10% | 2-min window |

**PagerDuty escalation path:** payment-gateway on-call → payments platform lead → VP Engineering

---

## Incident Command Roles

| Role | Owner | Responsibility |
|---|---|---|
| Incident Commander | On-call SRE | Coordinates response, owns timeline |
| Subject Matter Expert | Payments platform engineer | Diagnoses root cause |
| Communications Lead | Product on-call | Customer status page, merchant comms |
| Scribe | Incident Commander or assistant | Records timeline in war-room doc |

---

## Triage (0–5 min)

```bash
# 1. Confirm scope — is this latency or errors, or both?
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=histogram_quantile(0.99,
    rate(http_request_duration_seconds_bucket{service="payment_gateway"}[5m]))' \
  | jq '.data.result[].value[1]'

# 2. Is card_rails the upstream cause?
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=rate(http_requests_total{
    service="payment_gateway", status_code=~"5.."}[5m])
    / rate(http_requests_total{service="payment_gateway"}[5m])'

# 3. Check recent deployments (last 30 min)
kubectl rollout history deployment/payment-gateway -n payments
kubectl rollout history deployment/card-rails-adapter -n payments

# 4. Check if circuit breaker is still closed (latency means it hasn't tripped yet)
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=circuit_breaker_state{service="payment_gateway"}'
```

**Decision gate at 5 min:**
- If p99 > 5s AND error_rate > 15% → escalate to SEV-1 immediately
- If deployment was in last 30 min → go to **Rollback path**
- If no deployment → go to **Diagnosis path**

---

## Diagnosis (5–15 min)

### Check: Is it a card rails throttle?

```bash
# Card rails log pattern — look for 429 and timeout errors
kubectl logs -l app=card-rails-adapter -n payments --since=10m \
  | jq 'select(.http_status == 429 or .http_status == 504)'

# Check card rails own latency (separate from payment_gateway's view)
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=histogram_quantile(0.99,
    rate(http_request_duration_seconds_bucket{service="card_rails"}[5m]))'
```

### Check: Is it a connection pool issue?

```bash
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=db_connection_pool_active{db="postgres_primary"}
    / db_connection_pool_max{db="postgres_primary"}'
# > 0.85 = pool pressure contributing to latency
```

### Check: Is it a specific transaction type or all traffic?

```bash
# Grafana: filter payment_transactions_total by transaction_type
# Looking for: is latency isolated to card_present, card_not_present, or ACH?
kubectl logs -l app=payment-gateway -n payments --since=5m \
  | jq 'select(.level == "ERROR") | .payment.transaction_id, .error.message' \
  | head -40
```

### Trace correlation

```bash
# Find a slow trace ID from logs
SLOW_TRACE=$(kubectl logs -l app=payment-gateway -n payments --since=5m \
  | jq -r 'select(.duration_ms > 1500) | .trace_id' | head -1)

# Open in Grafana Tempo: http://localhost:3000/explore → Tempo → trace ID
echo "Trace: $SLOW_TRACE"
```

---

## Remediation

### Option A: Card rails throttle (most common)

```bash
# 1. Enable payment request queuing (built-in backpressure)
kubectl set env deployment/payment-gateway \
  CARD_RAILS_QUEUE_ENABLED=true \
  CARD_RAILS_QUEUE_MAX=2000 \
  -n payments

# 2. Reduce outbound RPS to card rails to match their capacity
kubectl set env deployment/payment-gateway \
  CARD_RAILS_MAX_RPS=200 \
  -n payments

# 3. Enable graceful degradation (return pending status instead of error)
kubectl set env deployment/payment-gateway \
  PAYMENT_DEGRADE_TO_PENDING=true \
  -n payments

# Verify: watch error rate drop within 2 min
watch -n 5 'curl -s http://localhost:9090/api/v1/query \
  --data-urlencode "query=rate(http_requests_total{
    service=\"payment_gateway\",status_code=~\"5..\"}[2m])" \
  | jq .data.result[].value[1]'
```

### Option B: Deployment rollback

```bash
# Identify the breaking version
kubectl rollout history deployment/payment-gateway -n payments

# Roll back
kubectl rollout undo deployment/payment-gateway -n payments
kubectl rollout status deployment/payment-gateway -n payments --timeout=120s
```

### ⚠ Human approval required for Option C

**Option C: Route traffic to backup card processor**

This requires approval from Payments Platform Lead + Finance sign-off.
Backup processor has different fee structure and may affect reconciliation.

```bash
# Only execute after approval documented in war-room doc
kubectl set env deployment/payment-gateway \
  CARD_RAILS_PROVIDER=backup \
  -n payments
```

---

## Verification (post-remediation)

```bash
# p99 latency recovering
kubectl exec -n monitoring deployment/prometheus -- \
  promtool query instant http://localhost:9090 \
  'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{service="payment_gateway"}[2m]))'

# Error rate below baseline
# Target: < 0.5% within 5 min of remediation
```

---

## Rollback of remediation

```bash
# Undo queue + rate limit changes
kubectl set env deployment/payment-gateway \
  CARD_RAILS_QUEUE_ENABLED=false \
  CARD_RAILS_MAX_RPS="" \
  PAYMENT_DEGRADE_TO_PENDING=false \
  -n payments
```

---

## Communications templates

**Status page (customer-facing):**
> We are currently experiencing delays with some payment processing. Transactions may take longer than usual to complete. No payments will be lost. We will update this status within 15 minutes.

**Internal Slack (#incidents):**
> 🔴 SEV-2 | payment_gateway p99 latency {{VALUE}}ms (SLO: 500ms) | IC: @{{name}} | War room: {{link}} | Timeline: T+{{minutes}}min

---

## Post-incident requirements

- [ ] Postmortem filed within 48h
- [ ] PCI-DSS Req 10.7 audit log exported and retained
- [ ] SLO error budget reviewed — if < 20% remaining, freeze non-critical deploys
- [ ] Card rails SLA review with vendor if throttle was their fault
- [ ] Runbook updated if new failure pattern discovered
