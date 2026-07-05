"""Per-user sliding-window rate limiting for the cost-abuse surfaces
(OpenAI calls, GitHub scans).

In-memory and per-process — correct for the single-instance deploy.
When gunicorn workers arrive the store must move to something shared
(Redis) or limits multiply by the worker count; see ROADMAP.md.

Apply *inside* login_required so g.user is set:

    @app.route(...)
    @login_required
    @rate_limit("messages", limit=20, per_seconds=60)
    def view(...):
"""
import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import g, jsonify

_lock = threading.Lock()
_hits = defaultdict(deque)  # (user_id, bucket) -> monotonic times of recent hits

DEFAULT_MESSAGE = "You're sending requests too quickly. Wait a moment and try again."


def rate_limit(bucket, limit, per_seconds, message=DEFAULT_MESSAGE):
    """At most `limit` requests per `per_seconds` window, per user per
    bucket. Buckets are shared across routes that share a name — scan
    and rescan count against the same budget. Rejected requests don't
    consume budget, but requests that later fail validation do: abuse
    doesn't get free retries."""
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            key = (g.user.id, bucket)
            now = time.monotonic()
            with _lock:
                q = _hits[key]
                while q and q[0] <= now - per_seconds:
                    q.popleft()
                if len(q) >= limit:
                    retry_after = max(1, int(q[0] + per_seconds - now) + 1)
                    resp = jsonify({"error": message})
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp
                q.append(now)
            return view(*args, **kwargs)
        return wrapped
    return decorator
