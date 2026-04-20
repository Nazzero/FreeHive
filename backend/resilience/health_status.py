"""
health_status.py — FreeHive Resilience

Tracks per-provider health state. Exposes status for the frontend to display
degraded-mode banners when fallbacks are active.
"""

import logging
import time
from enum import Enum

logger = logging.getLogger(__name__)


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"           # Primary strategy working
    DEGRADED = "degraded"         # Using fallback strategy
    DOWN = "down"                 # All strategies exhausted
    UNKNOWN = "unknown"           # Not yet tested


class ProviderHealth:
    __slots__ = ("provider", "status", "active_strategy", "message", "since", "strategies")

    def __init__(self, provider: str):
        self.provider = provider
        self.status = ProviderStatus.UNKNOWN
        self.active_strategy: str | None = None
        self.message: str = ""
        self.since: float = time.time()
        self.strategies: list[dict] = []

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "active_strategy": self.active_strategy,
            "message": self.message,
            "since": self.since,
            "strategies": self.strategies,
        }


class HealthMonitor:
    """Singleton health monitor for all providers."""

    def __init__(self):
        self._providers: dict[str, ProviderHealth] = {}

    def update(
        self,
        provider: str,
        status: ProviderStatus,
        active_strategy: str | None = None,
        message: str = "",
        strategies: list[dict] | None = None,
    ):
        if provider not in self._providers:
            self._providers[provider] = ProviderHealth(provider)

        health = self._providers[provider]
        old_status = health.status

        health.status = status
        health.active_strategy = active_strategy
        health.message = message
        health.since = time.time()
        if strategies:
            health.strategies = strategies

        if old_status != status:
            logger.info(
                "[health] %s: %s → %s (strategy: %s, msg: %s)",
                provider, old_status.value, status.value, active_strategy, message,
            )

    def mark_healthy(self, provider: str, strategy: str = "primary"):
        self.update(provider, ProviderStatus.HEALTHY, active_strategy=strategy)

    def mark_degraded(self, provider: str, strategy: str, message: str = ""):
        self.update(provider, ProviderStatus.DEGRADED, active_strategy=strategy, message=message)

    def mark_down(self, provider: str, message: str = ""):
        self.update(provider, ProviderStatus.DOWN, message=message)

    def get_status(self, provider: str) -> dict:
        if provider in self._providers:
            return self._providers[provider].to_dict()
        return ProviderHealth(provider).to_dict()

    def get_all(self) -> dict:
        return {
            name: health.to_dict()
            for name, health in self._providers.items()
        }

    def get_healthy_providers(self) -> list[str]:
        return [
            name for name, health in self._providers.items()
            if health.status in (ProviderStatus.HEALTHY, ProviderStatus.DEGRADED)
        ]


# Global singleton
health_monitor = HealthMonitor()
