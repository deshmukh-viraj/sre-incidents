"""
learning/distill.py
-------------------
the compounding-learning loop (Bet 5 / D-06), least-complex version:

- TERMINAL incident -> a DETERMINISTIC draft (no LLM: the terminal state already
  carries the structured diagnosis; an LLM summary adds cost and variance).
  Drafts land in a human-gated queue — a SECURITY gate, not UX (memory
  poisoning: an attacker who can shape one incident's narrative must not write
  the agent's future memory unreviewed).
- Human approves via the API -> the draft becomes agent memory: appended to the
  KG (append_past_incident) so query_topology/past-incident retrieval sees it.
- Retrieval for new investigations scores approved memory by fingerprint
  similarity and injects it as LOW-TRUST data (never instructions).

One memory, two surfaces: KG + FAISS. No second memory framework.
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_QUEUE_PATH = Path(os.getenv(
    "LEARNING_QUEUE_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "learning_queue.jsonl"),
))
_LOCK = threading.Lock()


def fingerprint(alert_name: str, service: str, signals: dict) -> str:
    """stable identity of an incident SHAPE: alert class + service + which
    signals were present (coarse buckets, not raw values — the shape repeats,
    the exact numbers do not)."""
    keys = sorted(
        k for k, v in (signals or {}).items()
        if v is not None and not k.startswith("_") and not k.endswith("summaries")
    )
    coarse = []
    for k in keys:
        v = (signals or {}).get(k)
        if isinstance(v, (int, float)):
            coarse.append(f"{k}:{'high' if v else 'zero'}")
        else:
            coarse.append(f"{k}:present")
    raw = f"{alert_name}|{service}|{';'.join(coarse)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def draft_from_state(state: dict) -> dict:
    """deterministic draft from a terminal incident state. Never auto-applies."""
    raw = state.get("raw_signals") or {}
    return {
        "draft_id": f"DR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-"
                    f"{fingerprint(state.get('alert_name', ''), (state.get('affected_services') or [''])[0], raw)}",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "pending",  # pending -> approved -> applied  (or rejected)
        "alert_name": state.get("alert_name", ""),
        "service": (state.get("affected_services") or [""])[0],
        "root_cause": state.get("root_cause") or "",
        "action_taken": state.get("action_taken") or "",
        "resolution_cause": state.get("resolution_cause") or "",
        "verified": bool(state.get("verified")),
        "severity": state.get("severity"),
        "fingerprint": fingerprint(state.get("alert_name", ""), (state.get("affected_services") or [""])[0], raw),
        "provenance": {
            "evidence_lines": len((state.get("hypotheses") or [{}])[0].get("evidence", [])) if state.get("hypotheses") else 0,
            "policy_rejections": len(state.get("policy_rejections") or []),
        },
    }


def enqueue(state: dict) -> None:
    """queue a draft for human review. never raises, never auto-applies."""
    try:
        d = draft_from_state(state)
        if not d["root_cause"] and not d["action_taken"]:
            return  # nothing teachable in an empty terminal state
        with _LOCK:
            _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
            # dedupe: same fingerprint already queued or applied -> skip
            for existing in _read_all():
                if existing.get("fingerprint") == d["fingerprint"] and existing.get("status") != "rejected":
                    return
            with open(_QUEUE_PATH, "a") as f:
                f.write(json.dumps(d, default=str) + "\n")
        print(f"[distill] draft {d['draft_id']} queued for human review "
              f"(cause={d['resolution_cause']}, verified={d['verified']})")
    except Exception as e:
        print(f"[distill] enqueue failed (non-fatal): {e}")


def _read_all() -> list:
    if not _QUEUE_PATH.exists():
        return []
    out = []
    with open(_QUEUE_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def pending_drafts() -> list:
    return [d for d in _read_all() if d.get("status") == "pending"]


def approve_draft(draft_id: str, approver: str, edited_root_cause: str = None) -> dict:
    """human approval: mark approved and apply to the KG (agent memory)."""
    with _LOCK:
        drafts = _read_all()
        target = None
        for d in drafts:
            if d.get("draft_id") == draft_id and d.get("status") == "pending":
                target = d
                break
        if target is None:
            return {"ok": False, "detail": f"pending draft {draft_id} not found"}
        target["status"] = "approved"
        target["approved_by"] = approver
        target["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if edited_root_cause:
            target["root_cause"] = edited_root_cause  # human correction wins
        with open(_QUEUE_PATH, "w") as f:
            for d in drafts:
                f.write(json.dumps(d, default=str) + "\n")
    # apply to the agent's memory (KG); FAISS ingestion rides the runbook refresh
    from src.tools.kg_tool import append_past_incident
    append_past_incident(
        incident_id=f"learned-{target['draft_id']}",
        alert_name=target["alert_name"] or "unknown",
        service=target["service"] or "unknown",
        root_cause=target["root_cause"] or "unknown",
        action_taken=target["action_taken"] or "unknown",
        business_mttr_seconds=None,
        success=bool(target["verified"]),
        resolution_cause=target["resolution_cause"] or None,
        severity=target.get("severity"),
    )
    try:
        from src.utils.audit import audit_event
        audit_event(target.get("service") or "unknown", approver, "learning_draft_approved",
                    {"draft_id": draft_id, "fingerprint": target["fingerprint"]})
    except Exception:
        pass
    return {"ok": True, "draft_id": draft_id, "applied_to": "kg"}


def retrieve_similar(alert_name: str, service: str, signals: dict, k: int = 1) -> list:
    """approved memory that looks like THIS incident, scored by fingerprint
    overlap. LOW-TRUST: callers render the result as data, never instructions."""
    this_fp = fingerprint(alert_name, service, signals)
    scored = []
    for d in _read_all():
        if d.get("status") != "approved":
            continue
        score = 0
        if d.get("fingerprint") == this_fp:
            score += 5
        if d.get("alert_name") == alert_name:
            score += 3
        if d.get("service") == service:
            score += 2
        if score:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:k]]
