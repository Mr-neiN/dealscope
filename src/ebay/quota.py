"""Hard daily API call ledger.

Every HTTP attempt is recorded before it is made. The ledger is deliberately
conservative: retries and failed requests count against the daily budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol


class QuotaExceeded(RuntimeError):
    """The scanner would exceed its configured daily API budget."""


class QuotaRepository(Protocol):
    def calls_today(self, day: str, endpoint: str | None = None) -> int: ...
    def record_api_call(self, day: str, endpoint: str, marketplace: str) -> None: ...


@dataclass(frozen=True)
class QuotaStatus:
    day: str
    used: int
    budget: int
    reserve: int

    @property
    def remaining(self) -> int:
        return max(self.budget - self.used, 0)

    @property
    def search_remaining(self) -> int:
        """Calls available for scheduled searches after keeping the reserve."""
        return max(self.budget - self.reserve - self.used, 0)


class QuotaLedger:
    def __init__(
        self,
        repository: QuotaRepository,
        daily_budget: int,
        reserve_calls: int,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if daily_budget <= 0:
            raise ValueError("daily_budget must be positive")
        self._repo = repository
        self._budget = daily_budget
        self._reserve = max(0, reserve_calls)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._day = self._today()
        self._used = self._repo.calls_today(self._day)

    def _today(self) -> str:
        return self._clock().strftime("%Y-%m-%d")

    def _rollover(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = self._repo.calls_today(today, None)

    def status(self) -> QuotaStatus:
        self._rollover()
        return QuotaStatus(self._day, self._used, self._budget, self._reserve)

    def consume(self, endpoint: str, marketplace: str = "") -> None:
        """Record one HTTP attempt or raise before it is sent."""
        self._rollover()
        if self._used >= self._budget:
            raise QuotaExceeded(
                f"daily API budget exhausted ({self._used}/{self._budget} calls on {self._day})"
            )
        self._repo.record_api_call(self._day, endpoint, marketplace)
        self._used += 1

    def can_spend(self, calls: int = 1) -> bool:
        self._rollover()
        return self._used + calls <= self._budget

    def detail_calls_today(self) -> int:
        self._rollover()
        return self._repo.calls_today(self._day, "browse_item")
