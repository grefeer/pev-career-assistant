from __future__ import annotations

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.adapters.bytedance import ByteDanceCrawlAdapter
from backend.app.services.job_discovery.adapters.alibaba_spa import AlibabaSPAAdapter
from backend.app.services.job_discovery.adapters.feishu import FeishuCrawlAdapter
from backend.app.services.job_discovery.adapters.inovance import InovanceCrawlAdapter
from backend.app.services.job_discovery.adapters.moka import MokaCrawlAdapter
from backend.app.services.job_discovery.adapters.mioffice import MiofficeCrawlAdapter
from backend.app.services.job_discovery.adapters.pdd import PddCrawlAdapter
from backend.app.services.job_discovery.adapters.xiaohongshu import (
    XiaohongshuCrawlAdapter,
)

__all__ = [
    "DomainAdapter",
    "ByteDanceCrawlAdapter",
    "AlibabaSPAAdapter",
    "FeishuCrawlAdapter",
    "InovanceCrawlAdapter",
    "MokaCrawlAdapter",
    "MiofficeCrawlAdapter",
    "PddCrawlAdapter",
    "XiaohongshuCrawlAdapter",
]
