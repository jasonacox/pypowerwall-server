"""
Request Statistics Tracker

Tracks request counts, errors, and timing for the /stats endpoint.
Provides backward compatibility with the original pypowerwall proxy statistics.
"""
import time
from collections import defaultdict
from threading import Lock
from typing import Dict


class StatsTracker:
    """Thread-safe request statistics tracker."""

    def __init__(self):
        self._lock = Lock()
        self._start_time = time.time()
        self._clear_time = time.time()

        # Request counters
        self._gets = 0
        self._posts = 0
        self._errors = 0
        self._timeouts = 0

        # Per-endpoint counters
        self._uri_counts: Dict[str, int] = defaultdict(int)

    def record_request(self, method: str, path: str, status_code: int = 200):
        """Record a request.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path
            status_code: HTTP status code (only track URI for 200-399)
        """
        with self._lock:
            if method == "GET":
                self._gets += 1
            elif method == "POST":
                self._posts += 1

            # Only track URIs for successful requests (not 404s)
            # This prevents memory exhaustion from DDOS attacks with random URLs
            if 200 <= status_code < 400:
                # Normalize path for tracking:
                # 1. Remove query parameters (?key=value)
                # 2. Remove trailing slashes (except for root /)
                # This ensures /monitor and /monitor?something are tracked the same
                base_path = path.split("?")[0]
                if base_path != "/" and base_path.endswith("/"):
                    base_path = base_path.rstrip("/")
                self._uri_counts[base_path] += 1

    def record_error(self):
        """Record an error."""
        with self._lock:
            self._errors += 1

    def record_timeout(self):
        """Record a timeout."""
        with self._lock:
            self._timeouts += 1

    def get_stats(self) -> dict:
        """Get current statistics."""
        with self._lock:
            return {
                "gets": self._gets,
                "posts": self._posts,
                "errors": self._errors,
                "timeout": self._timeouts,
                "uri": dict(self._uri_counts),
                "start": int(self._start_time),
                "clear": int(self._clear_time),
            }

    def reset(self):
        """Reset counters (keep start_time)."""
        with self._lock:
            self._clear_time = time.time()
            self._gets = 0
            self._posts = 0
            self._errors = 0
            self._timeouts = 0
            self._uri_counts.clear()


# Global singleton instance
stats_tracker = StatsTracker()
