"""DomainAdapter -- abstract base for domain-specific fast-path adapters."""
from __future__ import annotations

from abc import ABC, abstractmethod

from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class DomainAdapter(ABC):
    """Base class for domain-specific job discovery adapters.

    Each adapter provides an optimal execution path for a known domain,
    typically calling internal APIs directly rather than navigating pages.
    """

    url_pattern: str = ""

    @abstractmethod
    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: "StrategyRecord",
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        """Execute job discovery for this domain.

        Must call trajectory.record_step() for each significant operation.
        On failure, the trajectory buffer already contains partial progress
        for Supervisor takeover.
        """
        ...

    @abstractmethod
    def validate(self, url: str) -> bool:
        """Quick check whether *url* is still reachable/valid for this adapter."""
        ...
