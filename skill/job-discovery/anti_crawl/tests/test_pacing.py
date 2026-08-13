from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from anti_crawl.pacing import PacingConfig, PacingController, PacingViolation, load_proxy_config


def test_waits_at_least_min_interval(tmp_path: Path) -> None:
    pacing = PacingController("t1", PacingConfig(base_interval_s=(0.05, 0.06)), tmp_path)
    start = time.monotonic()
    for _ in range(5):
        pacing.wait_before_request()
        pacing.record_request()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.20, f"5 waits should take >=0.20s, took {elapsed:.3f}s"


def test_daily_cap_raises_violation(tmp_path: Path) -> None:
    pacing = PacingController("t2", PacingConfig(base_interval_s=(0.01, 0.01), max_pages_per_day=2), tmp_path)
    pacing.wait_before_request()
    pacing.record_request()
    pacing.wait_before_request()
    pacing.record_request()
    assert pacing.remaining_pages_today() == 0
    with pytest.raises(PacingViolation) as exc:
        pacing.wait_before_request()
    assert exc.value.reason == "daily_cap_reached"


def test_state_persists_across_instances(tmp_path: Path) -> None:
    PacingController("t3", PacingConfig(base_interval_s=(0.01, 0.01)), tmp_path).record_request()
    reloaded = PacingController("t3", PacingConfig(base_interval_s=(0.01, 0.01)), tmp_path)
    assert reloaded.remaining_pages_today() == 499


def test_backoff_waits_schedule(tmp_path: Path) -> None:
    # 间隔用 0.5s 量级：Windows 定时器粒度 ~15.6ms，过小的 sleep 断言会间歇失败
    pacing = PacingController("t4", PacingConfig(backoff_schedule_s=(0.5, 0.6, 0.7)), tmp_path)
    start = time.monotonic()
    pacing.wait_on_backoff(1)
    assert time.monotonic() - start >= 0.45
    start = time.monotonic()
    pacing.wait_on_backoff(9)  # 越界取最后一个
    assert time.monotonic() - start >= 0.65


def test_proxy_config_gating(tmp_path: Path) -> None:
    assert load_proxy_config(None) is None
    p = tmp_path / "proxy.json"
    p.write_text(json.dumps({"enabled": False, "server": "http://127.0.0.1:8888"}), encoding="utf-8")
    assert load_proxy_config(p) is None
    p.write_text(json.dumps({"enabled": True, "server": "http://127.0.0.1:8888", "username": "", "password": ""}), encoding="utf-8")
    assert load_proxy_config(p) == {"server": "http://127.0.0.1:8888"}
