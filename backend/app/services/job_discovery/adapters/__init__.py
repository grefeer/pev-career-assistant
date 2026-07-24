from __future__ import annotations

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.adapters.alibaba_spa import AlibabaSPAAdapter
from backend.app.services.job_discovery.adapters.moka import MokaCrawlAdapter

__all__ = [
    "DomainAdapter",
    "AlibabaSPAAdapter",
    "MokaCrawlAdapter",
]
