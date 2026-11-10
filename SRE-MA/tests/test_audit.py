"""self-checks for the tamper-evident audit chain (security gap 6)."""

from src.utils import audit


def _fresh_chain(tmp_path, monkeypatch):
    p = tmp_path / "audit_chain.jsonl"
    monkeypatch.setattr(audit, "CHAIN_PATH", p)
    return p


def test_chain_appends_and_verifies(tmp_path, monkeypatch):
    _fresh_chain(tmp_path, monkeypatch)
    for i in range(3):
        audit.audit_event("INC-A", "policy_validator", "policy_decision", {"i": i})
    res = audit.verify_chain()
    assert res["ok"] is True and res["records"] == 3


def test_tampered_record_breaks_the_chain(tmp_path, monkeypatch):
    p = _fresh_chain(tmp_path, monkeypatch)
    audit.audit_event("INC-A", "executor", "execution_attribution", {"verified": True})
    audit.audit_event("INC-A", "executor", "execution_attribution", {"verified": False})
    lines = p.read_text().splitlines()
    rec = __import__("json").loads(lines[1])
    rec["data"]["verified"] = True  # someone edits history
    lines[1] = __import__("json").dumps(rec, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    res = audit.verify_chain()
    assert res["ok"] is False and res["first_bad_seq"] == 2


def test_dropped_record_breaks_the_chain(tmp_path, monkeypatch):
    p = _fresh_chain(tmp_path, monkeypatch)
    for i in range(3):
        audit.audit_event("INC-A", "api", "approval_decision", {"i": i})
    lines = p.read_text().splitlines()
    del lines[1]  # remove the middle record silently
    p.write_text("\n".join(lines) + "\n")
    res = audit.verify_chain()
    assert res["ok"] is False
