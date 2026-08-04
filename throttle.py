"""Shared rate limiter + 429-aware retry for the bot's requests.Session.

The repo's wiki._get treats an HTTP 429 as a generic network error: 4 attempts
with 1/2/4s backoff and no Retry-After handling. Under parallel load against
ml.wikipedia that exhausts the retries and the article is lost. This wraps the
session so every request across all threads is spaced out, and a 429 or 503
waits for the server-supplied Retry-After before trying again.

Import and call install() BEFORE any wiki call.
"""

import random
import threading
import time

_lock = threading.Lock()
_next_slot = [0.0]
_interval = [0.6]


def set_interval(seconds):
    """Change request spacing after install (e.g. once logged in)."""
    _interval[0] = seconds


def install(session, min_interval=0.6, max_attempts=8, max_backoff=45.0):
    _interval[0] = min_interval
    original = session.request

    def throttled(method, url, **kwargs):
        for attempt in range(max_attempts):
            # global spacing across threads
            with _lock:
                now = time.monotonic()
                wait = max(0.0, _next_slot[0] - now)
                _next_slot[0] = max(now, _next_slot[0]) + _interval[0]
            if wait:
                time.sleep(wait)

            resp = original(method, url, **kwargs)
            if resp.status_code not in (429, 503):
                return resp

            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 2.0 * (2 ** attempt)
            # A server-supplied Retry-After can be very large; cap it so one
            # throttled request cannot stall the whole run for an hour.
            delay = min(delay, max_backoff) + random.uniform(0, 1.5)
            print(f"  [throttle] HTTP {resp.status_code}, sleeping {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_attempts})", flush=True)
            # back everyone off, not just this thread
            with _lock:
                _next_slot[0] = max(_next_slot[0], time.monotonic() + delay)
            time.sleep(delay)
        return resp  # caller's raise_for_status will surface it

    session.request = throttled
    return session
