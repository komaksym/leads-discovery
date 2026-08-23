"""Provider usage, cost aggregation, and replayable independent budget helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict

from leads_discovery.models import UsageEvent


class UsageTotalsDict(TypedDict):
    """Describe serialized provider or run-level usage totals."""

    request_count: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    exact_cost_usd: float | None


class UsageSummary(TypedDict):
    """Describe the complete serialized usage summary."""

    providers: dict[str, UsageTotalsDict]
    total: UsageTotalsDict


@dataclass(slots=True)
class _UsageTotals:
    """Accumulate numeric usage fields without losing provider boundaries."""

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    estimated_cost_event_count: int = 0
    exact_cost_usd: float = 0.0
    exact_cost_event_count: int = 0
    event_count: int = 0

    def add(self, event: UsageEvent) -> None:
        """Add one usage event to these totals."""
        self.event_count += 1
        self.request_count += event.request_count
        self.input_tokens += event.input_tokens
        self.output_tokens += event.output_tokens
        if event.estimated_cost_usd is not None:
            self.estimated_cost_usd += event.estimated_cost_usd
            self.estimated_cost_event_count += 1
        if event.exact_cost_usd is not None:
            self.exact_cost_usd += event.exact_cost_usd
            self.exact_cost_event_count += 1

    def estimated(self) -> float | None:
        """Return complete estimated spend, or unknown when any event omitted its estimate."""
        if not self.event_count:
            return None
        if self.estimated_cost_event_count != self.event_count:
            return None
        return round(self.estimated_cost_usd, 10)

    def to_dict(self) -> UsageTotalsDict:
        """Return totals without presenting partial cost information as complete."""
        return {
            "request_count": self.request_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated(),
            "exact_cost_usd": (
                round(self.exact_cost_usd, 10)
                if self.event_count and self.exact_cost_event_count == self.event_count
                else None
            ),
        }


class CostTracker:
    """Aggregate provider usage events for reporting and independent budget decisions."""

    def __init__(self, events: Iterable[UsageEvent] = ()) -> None:
        """Create a tracker and replay any already persisted usage events."""
        self._providers: dict[str, _UsageTotals] = {}
        self._total = _UsageTotals()
        for event in events:
            self.record(event)

    def record(self, event: UsageEvent) -> None:
        """Record one provider usage event in provider and run-level totals."""
        totals = self._providers.setdefault(event.provider, _UsageTotals())
        totals.add(event)
        self._total.add(event)

    def provider_estimated_spend(self, provider: str) -> float | None:
        """Return replayed estimated spend for one provider without pooling other providers."""
        totals = self._providers.get(provider)
        return 0.0 if totals is None else totals.estimated()

    def summary(self) -> UsageSummary:
        """Return a JSON-friendly summary grouped by provider plus the run total."""
        return {
            "providers": {
                provider: totals.to_dict() for provider, totals in sorted(self._providers.items())
            },
            "total": self._total.to_dict(),
        }
