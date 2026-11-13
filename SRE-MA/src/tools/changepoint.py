"""
tools/changepoint.py
--------------------
Finds the exact moment a metric unexpectedly jumps or drops.

wow it helps:
when an incident happens, this tool looks at the metric history and pinpoints
the exact timestamp when the issue started (the level shift). The AI agent uses
this timestamp as hard evidence to verify root causes, so any explanation that
doesn't match this start time can be ruled out.

safe and cautious:
if the data is too short, mostly flat, or the change is too small to be certain,
it returns None instead of guessing.
"""

from typing import Any, Dict, List, Optional

import numpy as np

MIN_SAMPLES = 10          # below this, detection is noise
MIN_SPLIT_FRACTION = 0.2  # ignore splits in the first/last 20% 
MIN_SIGMA = 3.0           # required shift size in pooled-sigma units


def detect_changepoint(
    series: List[Any],
    min_sigma: float = MIN_SIGMA,
) -> Optional[Dict[str, Any]]:
    """
    returns {t_onset, onset_iso, minutes_ago, sigma, direction, before, after}
    where t_onset is the first timestamp of the NEW level (the split point).
    """
    if not series or len(series) < MIN_SAMPLES:
        return None
    ts = np.array([float(t) for t, _ in series], dtype=float)
    vals = np.array([float(v) for _, v in series], dtype=float)
    if not np.all(np.isfinite(vals)):
        vals = np.nan_to_num(vals, nan=0.0, posinf=0.0, neginf=0.0)

    n = len(vals)
    lo = max(2, int(n * MIN_SPLIT_FRACTION))
    hi = n - lo
    if hi <= lo:
        return None

    #scan splits, score |mean_after - mean_before|
    prefix_sum = np.cumsum(vals)
    total = prefix_sum[-1]
    best_score, best_k = 0.0, None
    best_before = best_after = 0.0
    for k in range(lo, hi):
        before = prefix_sum[k - 1] / k
        after = (total - prefix_sum[k - 1]) / (n - k)
        var_b = float(np.var(vals[:k])) if k > 1 else 0.0
        var_a = float(np.var(vals[k:])) if n - k > 1 else 0.0
        pooled = (var_b * (k - 1) + var_a * (n - k - 1)) / (n - 2) if n > 2 else 0.0
        std = float(np.sqrt(max(pooled, 1e-12)))
        score = abs(after - before) / std
        if score > best_score:
            best_score, best_k = score, k
            best_before, best_after = before, after

    if best_k is None or best_score < min_sigma:
        return None

    #the level must ALSO sit clear of the baseline spread (a wide-flat series
    # can produce a decent normalized score with a trivially small shift)
    base = vals[:best_k]
    if abs(best_after - best_before) < 2.0 * float(np.std(base) + 1e-12):
        return None

    onset_ts = ts[best_k]
    import datetime as dt
    return {
        # float(): round() on a numpy scalar stays numpy and the msgpack
        # checkpointer cannot serialize it
        "t_onset": float(onset_ts),
        "onset_iso": dt.datetime.fromtimestamp(onset_ts, dt.timezone.utc).isoformat(timespec="seconds"),
        "minutes_ago": max(0, int((ts[-1] - onset_ts) / 60.0)),
        "sigma": round(float(best_score), 2),
        "direction": "up" if best_after > best_before else "down",
        "before": round(float(best_before), 6),
        "after": round(float(best_after), 6),
    }
