"""
clear_time.py
---------------
figures out recovery timestamps by evaluating prometheus history against thresholds.
"""

import json
import os
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

# Outcomes
CLEAR_SOURCE_RANGE = "range_backfill"
CLEAR_SOURCE_POLLED = "polled"
CLEAR_SOURCE_WEBHOOK = "webhook"
CLEAR_SOURCE_NONE = "never_cleared"


@dataclass
class ClearEvidence:
    """recovery evidence bucket for Gate 3 verification"""
    t_clear_true: Optional[float]
    clear_source: str
    healthy_run_len: int = 0
    samples_seen: int = 0
    per_action_healthy: bool = False
    per_action_trend_pct: Optional[float] = None
    already_healing: bool = False
    first_sample_ts: Optional[float] = None
    last_sample_ts: Optional[float] = None
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _is_healthy(val: float, threshold: float, comparison: str) -> bool:
    return val < threshold if comparison == "below" else val > threshold


def find_true_clear_time(
    samples: Sequence[tuple[float, float]],
    threshold: float,
    *,
    consecutive: int = 3,
    comparison: str = "below",
) -> ClearEvidence:
    """finds the first sustained healthy run in timestamped samples."""
    if consecutive < 1:
        raise ValueError("consecutive must be >= 1")

    ev = ClearEvidence(t_clear_true=None, clear_source=CLEAR_SOURCE_NONE, samples_seen=len(samples))
    if not samples:
        ev.notes.append("perometheus returned no samples for this time window")
        return ev

    ev.first_sample_ts, ev.last_sample_ts = samples[0][0], samples[-1][0]
    run, run_start_ts, best_run = 0, None, 0

    for ts, val in samples:
        if _is_healthy(val, threshold, comparison):
            if run == 0:
                run_start_ts = ts
            run += 1
            best_run = max(best_run, run)
            if run >= consecutive and ev.t_clear_true is None:
                ev.t_clear_true = run_start_ts
                ev.clear_source = CLEAR_SOURCE_RANGE
        else:
            run, run_start_ts = 0, None

    ev.healthy_run_len = best_run
    if ev.t_clear_true is None:
        ev.notes.append(
            f"never saw a sustained recovery. Longest healthy run was {best_run} sample(s), needed {consecutive}."
        )

    return ev


def analyse_pre_action(
    samples: Sequence[tuple[float, float]],
    threshold: float,
    *,
    comparison: str = "below",
    heal_frac: float = 0.20,
    monotone_tolerance: float = 0.05,
) -> tuple[bool, Optional[float], bool]:
    """determines if metric was already recovering prior to agent action"""
    if len(samples) < 2:
        return (False, None, False)

    vals = [v for _, v in samples]
    first, last = vals[0], vals[-1]
    denom = max(abs(first), 1e-9)
    trend = (first - last if comparison == "below" else last - first) / denom

    span = max(abs(max(vals) - min(vals)), 1e-9)
    improving_steps = 0
    regressions = 0
    for a, b in zip(vals, vals[1:]):
        delta = (a - b) if comparison == "below" else (b - a)
        if delta > 0:
            improving_steps += 1
        elif abs(delta) / span > monotone_tolerance:
            regressions += 1

    already_healing = trend > heal_frac and regressions == 0 and improving_steps > 0
    return (_is_healthy(last, threshold, comparison), trend, already_healing)


def _to_unix(ts) -> float:
    """helper to convert timestamps (int, float, str, datetime) to float epoch seconds"""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if isinstance(ts, datetime):
        return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()
    raise TypeError(f"unsupported timestamp type: {type(ts)!r}")


def query_range(
    expr: str,
    start,
    end,
    step: int = 5,
    *,
    base_url: Optional[str] = None,
    timeout: float = 10.0,
) -> list[tuple[float, float]]:

    """query prometheus range api using standard library urllib"""
    base_url = (base_url or os.getenv("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
    params = urllib.parse.urlencode(
        {"query": expr, "start": f"{_to_unix(start):.3f}", "end": f"{_to_unix(end):.3f}", "step": f"{step}s"}
    )

    with urllib.request.urlopen(f"{base_url}/api/v1/query_range?{params}", timeout=timeout) as fh:
        payload = json.loads(fh.read().decode())

    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus query_range failed: {payload.get('error')}")

    results = payload.get("data", {}).get("result", [])
    if not results:
        return []

    out = []
    for ts, val in results[0].get("values", []):
        try:
            out.append((float(ts), float(val)))
        except (TypeError, ValueError):
            continue
    return out


def build_clear_evidence(
    expr: str,
    threshold: float,
    t_action_start,
    t_action_end,
    t_now,
    *,
    step: int = 5,
    consecutive: int = 3,
    comparison: str = "below",
    pre_window_sec: int = 120,
    t_clear_polled=None,
    fetch: Optional[Callable[..., list[tuple[float, float]]]] = None,
    **fetch_kwargs,
) -> ClearEvidence:
    """Fetches metric window and evaluates clear evidence."""
    fetch = fetch or query_range
    a_start = _to_unix(t_action_start)
    a_end = _to_unix(t_action_end)

    samples = fetch(expr, a_start - pre_window_sec, _to_unix(t_now), step, **fetch_kwargs)

    # in simulation prometheus has no range history; fall back to polled timestamp
    # ceiling: if both are missing we truly can't attribute (gate 3 will fail correctly)
    if not samples and t_clear_polled is not None:
        ev = ClearEvidence(
            t_clear_true=_to_unix(t_clear_polled),
            clear_source=CLEAR_SOURCE_POLLED,
            samples_seen=0,
        )
        ev.notes.append("prometheus returned no range samples; using polled t_clear as fallback")
        return ev

    ev = find_true_clear_time(samples, threshold, consecutive=consecutive, comparison=comparison)
    #if range history lacks consecutive samples but live verification succeeded, fall back to polled t_clear
    if ev.t_clear_true is None and t_clear_polled is not None:
        ev.t_clear_true = _to_unix(t_clear_polled)
        ev.clear_source = CLEAR_SOURCE_POLLED
        ev.notes.append("prometheus range query returned insufficient samples; using polled t_clear as fallback")

    pre = [(ts, v) for ts, v in samples if ts <= a_start]
    per_action_healthy, trend, already_healing = analyse_pre_action(pre, threshold, comparison=comparison)
    ev.per_action_healthy = per_action_healthy
    ev.per_action_trend_pct = trend
    ev.already_healing = already_healing

    if ev.t_clear_true is not None and ev.t_clear_true < a_end:
        ev.notes.append(f"Recovery began {a_end - ev.t_clear_true:.1f}s BEFORE action completed.")
    return ev