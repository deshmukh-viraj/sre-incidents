"""
utils/audit.py
--------------
tamper-evident audit chain (security gap 6): append-only JSONL where every
record commits to the previous one via sha256. Doubles as third-party-auditable
attribution evidence (did the agent CAUSE the fix?) and as the forensics trail
for post-incident injection tracing.

Design:
- one global chain file (AUDIT_CHAIN_PATH, default <repo>/src/data/audit_chain.jsonl);
  a threading lock serializes appends (single-process deployment by design).
- record = {seq, ts, incident_id, actor, kind, data, prev_hash, hash};
  hash = sha256(prev_hash + sha256(canonical record-without-hash)).
  verify_chain() recomputes and detects any tamper, insertion or drop.
- failures to WRITE are printed and swallowed: the audit trail must never break
  the incident pipeline (it is evidence, not a control-plane dependency).
"""

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "audit_chain.jsonl"
CHAIN_PATH = Path(os.getenv("AUDIT_CHAIN_PATH", str(_DEFAULT_PATH)))
_LOCK = threading.Lock()


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _last_hash() -> str:
    if not CHAIN_PATH.exists():
        return "0" * 64
    try:
        with open(CHAIN_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 8192)
            f.seek(size - back)
            tail = f.read().decode(errors="replace").strip().splitlines()
            for line in reversed(tail):
                line = line.strip()
                if line:
                    return json.loads(line).get("hash", "0" * 64)
    except Exception as e:
        print(f"[audit] could not read chain tail: {e}")
    return "0" * 64


def audit_event(incident_id: str, actor: str, kind: str, data: dict) -> None:
    """append one linked record. never raises."""
    try:
        with _LOCK:
            CHAIN_PATH.parent.mkdir(parents=True, exist_ok=True)
            prev = _last_hash()
            seq = _last_seq() + 1
            body = {
                "seq": seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "incident_id": incident_id,
                "actor": actor,
                "kind": kind,
                "data": data,
                "prev_hash": prev,
            }
            digest = hashlib.sha256(
                (prev + hashlib.sha256(_canonical(body).encode()).hexdigest()).encode()
            ).hexdigest()
            body["hash"] = digest
            with open(CHAIN_PATH, "a") as f:
                f.write(_canonical(body) + "\n")
    except Exception as e:
        print(f"[audit] append failed (non-fatal): {e}")


def _last_seq() -> int:
    if not CHAIN_PATH.exists():
        return 0
    try:
        with open(CHAIN_PATH, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, 8192)
            f.seek(size - back)
            tail = f.read().decode(errors="replace").strip().splitlines()
            for line in reversed(tail):
                line = line.strip()
                if line:
                    return int(json.loads(line).get("seq", 0))
    except Exception:
        pass
    return 0


def verify_chain(path=None) -> dict:
    """recompute the whole chain. {ok, records, first_bad_seq, reason}."""
    p = Path(path or CHAIN_PATH)
    if not p.exists():
        return {"ok": True, "records": 0, "first_bad_seq": None, "reason": "empty chain"}
    prev = "0" * 64
    seq_expect = 0
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            seq_expect += 1
            try:
                rec = json.loads(line)
            except Exception as e:
                return {"ok": False, "records": seq_expect - 1, "first_bad_seq": seq_expect,
                        "reason": f"unparseable line: {e}"}
            body = {k: v for k, v in rec.items() if k != "hash"}
            digest = hashlib.sha256(
                (prev + hashlib.sha256(_canonical(body).encode()).hexdigest()).encode()
            ).hexdigest()
            if rec.get("hash") != digest or rec.get("prev_hash") != prev or rec.get("seq") != seq_expect:
                return {"ok": False, "records": seq_expect - 1, "first_bad_seq": seq_expect,
                        "reason": "hash or link mismatch (record tampered, inserted or dropped)"}
            prev = rec["hash"]
    return {"ok": True, "records": seq_expect, "first_bad_seq": None, "reason": ""}
