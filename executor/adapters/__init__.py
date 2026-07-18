"""Site adapter protocol, registry, and concrete adapters."""

from __future__ import annotations

from executor.adapters.iflytek.adapter import IflytekZhiyeAdapter
from executor.adapters.moka.adapter import MokaSiteAdapter
from executor.adapters.registry import register
from executor.adapters.xpeng.adapter import XpengFeishuAdapter


def register_builtin_adapters() -> None:
    """Register all built-in executable site adapters."""
    register(MokaSiteAdapter())
    register(XpengFeishuAdapter())
    register(IflytekZhiyeAdapter())


__all__ = ["register_builtin_adapters"]
