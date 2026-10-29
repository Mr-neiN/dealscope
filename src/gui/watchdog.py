"""Scanner heartbeat watchdog for the GUI container.

The scanner and the dashboard run in separate containers on purpose: if the
scan loop hangs or the scanner container exits, the dashboard keeps serving
stale data and the only sign is a small badge somebody has to open. The
GUI container shares the heartbeat file and outlives scanner crashes, so the
watchdog runs here: it pings Telegram when the heartbeat goes stale, repeats
the warning at a slow interval while things stay broken, and reports
recovery. When Telegram is not configured it logs instead.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def _to_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Watchdog:
    """Follow one heartbeat file and notify on stale/recovered transitions."""

    def __init__(
        self,
        heartbeat_path: Path,
        *,
        max_age_seconds: int = 900,
        interval_seconds: float = 60.0,
        nag_minutes: int = 240,
        notify: Callable[[str], Any] | None = None,
    ) -> None:
        self._path = heartbeat_path
        self._max_age = max(max_age_seconds, 1)
        self._interval = max(interval_seconds, 1.0)
        self._nag_interval = timedelta(minutes=max(nag_minutes, 1))
        self._notify = notify
        self._state: str | None = None
        self._last_nag: datetime | None = None
        self.stop = threading.Event()

    # ----------------------------------------------------------------- checks
    def age_seconds(self, now: datetime) -> int | None:
        """How old the heartbeat is, or None when absent/unreadable."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            updated = _to_utc(datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00")))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return max(0, int((_to_utc(now) - updated).total_seconds()))

    def check_once(self, now: datetime) -> str | None:
        """Evaluate the heartbeat; returns 'stale'/'recovered'/'nag' or None."""
        missing = not self._path.exists()
        age = self.age_seconds(now)
        stale = missing or (age is not None and age > self._max_age)
        new_state = "stale" if stale else "running"
        event: str | None = None
        if self._state is None:
            # Startup semantics: a stale heartbeat *file* means the scanner
            # ran and has gone quiet - alert. A missing file may simply mean
            # the scanner has not started yet - stay quiet until it recovers.
            self._state = new_state
            self._last_nag = now if stale else None
            if stale and age is not None:
                event = "stale"
        elif new_state != self._state:
            self._state = new_state
            self._last_nag = now if stale else None
            event = "stale" if stale else "recovered"
        elif stale and self._last_nag and (now - self._last_nag) >= self._nag_interval:
            self._last_nag = now
            event = "nag"
        message = self._message(event, age) if event else None
        if event and self._notify and message:
            try:
                self._notify(message)
            except Exception:  # watchdog failures must never break anything
                log.exception("watchdog notification failed")
        return event

    def _message(self, event: str | None, age: int | None) -> str | None:
        if age is None:
            age_text = "unknown"
        else:
            age_text = f"{age // 60}m" if age >= 60 else f"{age}s"
        if event == "stale":
            return (
                "Scanner heartbeat is stale (last update "
                f"{age_text} ago). The scan loop looks stopped: check "
                "'docker compose ps' and 'docker compose logs ebay-scanner'."
            )
        if event == "nag":
            return (
                f"Still no scanner heartbeat (last update {age_text} ago). "
                "The scanner has not recovered on its own."
            )
        if event == "recovered":
            return "Scanner is scanning again: heartbeat is current."
        return None

    # ------------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info(
            "heartbeat watchdog watching %s (stale after %ss, interval %ss)",
            self._path,
            self._max_age,
            self._interval,
        )
        while not self.stop.is_set():
            try:
                self.check_once(datetime.now(timezone.utc))
            except Exception:
                log.exception("watchdog check failed")
            self.stop.wait(self._interval)
