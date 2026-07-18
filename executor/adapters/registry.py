"""In-memory adapter registry with local circuit-breaker tracking.

The backend's ``site_adapters`` table is the authoritative source for
circuit-breaker state.  This local registry provides a fast fallback so
the executor can refuse to open a page even when the backend hasn't
yet acknowledged an error.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from executor.adapters.base import SiteAdapter


CIRCUIT_BREAKER_THRESHOLD = 5


@dataclass
class AdapterRegistryEntry:
    adapter: SiteAdapter
    status: str = "active"  # "active" | "circuit_breaker_open" | "deprecated"
    error_count: int = 0
    last_error: str | None = None
    last_error_at: float | None = None


ADAPTER_REGISTRY: dict[str, AdapterRegistryEntry] = {}


def register(adapter: SiteAdapter) -> None:
    """Register a site adapter in the local registry."""
    ADAPTER_REGISTRY[adapter.adapter_id] = AdapterRegistryEntry(
        adapter=adapter,
        status="active",
    )


def get(adapter_id: str) -> SiteAdapter | None:
    """Look up an adapter by ID."""
    entry = ADAPTER_REGISTRY.get(adapter_id)
    return entry.adapter if entry else None


def get_entry(adapter_id: str) -> AdapterRegistryEntry | None:
    """Look up a registry entry by ID."""
    return ADAPTER_REGISTRY.get(adapter_id)


def is_available(adapter_id: str) -> bool:
    """Return True if the adapter is registered and not circuit-broken."""
    entry = ADAPTER_REGISTRY.get(adapter_id)
    return entry is not None and entry.status == "active"


def record_error(adapter_id: str, error_detail: str) -> None:
    """Record an error for an adapter.

    After CIRCUIT_BREAKER_THRESHOLD consecutive errors the adapter is
    automatically disabled.
    """
    entry = ADAPTER_REGISTRY.get(adapter_id)
    if entry is None:
        return
    entry.error_count += 1
    entry.last_error = error_detail
    entry.last_error_at = time.time()
    if entry.error_count >= CIRCUIT_BREAKER_THRESHOLD:
        entry.status = "circuit_breaker_open"


def record_success(adapter_id: str) -> None:
    """Record a successful operation, reducing the error count.

    After 2 consecutive successes the circuit breaker closes again.
    """
    entry = ADAPTER_REGISTRY.get(adapter_id)
    if entry is None:
        return
    entry.error_count = max(0, entry.error_count - 1)
    if entry.status == "circuit_breaker_open" and entry.error_count < 2:
        entry.status = "active"


def reset_circuit_breaker(adapter_id: str) -> None:
    """Manually reset the circuit breaker for an adapter."""
    entry = ADAPTER_REGISTRY.get(adapter_id)
    if entry is None:
        return
    entry.status = "active"
    entry.error_count = 0
    entry.last_error = None
    entry.last_error_at = None


def list_active() -> list[str]:
    """Return adapter IDs of all active (non-broken) adapters."""
    return [
        aid
        for aid, entry in ADAPTER_REGISTRY.items()
        if entry.status == "active"
    ]
