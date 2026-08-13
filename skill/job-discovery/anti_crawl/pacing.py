"""礼貌限速：随机间隔、单页串行、指数退避、每日上限、代理配置预留位。"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "store" / "state"


class PacingViolation(Exception):
    """pacing 纪律被打破（如每日上限已到）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class PacingConfig:
    base_interval_s: tuple[float, float] = (2.0, 5.0)   # uniform(min, max)
    max_pages_per_day: int = 500
    backoff_schedule_s: tuple[int, ...] = (30, 60, 120)  # 403/429 退避
    max_backoff_attempts: int = 3


class PacingController:
    """每站一个控制器；状态落在 store/state/<site>.json，跨进程持久。"""

    def __init__(self, site_key: str, config: PacingConfig | None = None,
                 state_dir: Path | None = None) -> None:
        self.site_key = site_key
        self.config = config or PacingConfig()
        self._state_dir = state_dir or DEFAULT_STATE_DIR

    # -- 状态 -------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self._state_dir / f"{self.site_key}.json"

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {"date": "", "pages_fetched": 0}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"date": "", "pages_fetched": 0}

    def _save_state(self, state: dict[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _sync_today(self, state: dict[str, Any]) -> None:
        if state.get("date") != self._today():  # 跨天自动归零
            state["date"] = self._today()
            state["pages_fetched"] = 0

    # -- 公开 API ----------------------------------------------------------

    def remaining_pages_today(self) -> int:
        state = self._load_state()
        self._sync_today(state)
        return max(0, self.config.max_pages_per_day - int(state["pages_fetched"]))

    def wait_before_request(self) -> None:
        if self.remaining_pages_today() <= 0:
            raise PacingViolation("daily_cap_reached")
        low, high = self.config.base_interval_s
        time.sleep(random.uniform(low, high))

    def record_request(self) -> None:
        state = self._load_state()
        self._sync_today(state)
        state["pages_fetched"] = int(state["pages_fetched"]) + 1
        self._save_state(state)

    def wait_on_backoff(self, attempt: int) -> None:
        schedule = self.config.backoff_schedule_s
        seconds = schedule[min(max(attempt, 1), len(schedule)) - 1]
        time.sleep(seconds)


def load_proxy_config(path: Path | None) -> dict[str, Any] | None:
    """读 proxy.json 预留位：缺文件或 enabled!=True 一律返回 None（默认无代理）。

    返回形如 {"server": "http://host:port", "username": ..., "password": ...}，
    空值字段被剔除，可直接传给 playwright launch(proxy=...)。
    """
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not data.get("enabled", False) is True:
        return None
    proxy = {k: str(v).strip() for k, v in data.items() if k != "enabled" and v}
    return proxy or None
