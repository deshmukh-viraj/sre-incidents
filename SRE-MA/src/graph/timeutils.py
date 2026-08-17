from datetime import datetime, timezone

def parse_ts(ts) -> datetime:
    """parse an ISO timestamp to tz-aware UTC datetime"""
    if not ts: return None
    if isinstance(ts, datetime):
        dt = ts
    else:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    # ponytail: parse_ts always returns tz-aware UTC datetimes to match _utcnow()
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


    