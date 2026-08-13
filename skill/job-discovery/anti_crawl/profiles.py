"""profile 生命周期：每站独立 user_data_dir、稳定指纹元数据、登录态信号匹配。"""
from __future__ import annotations

import enum
import json
from datetime import datetime
from pathlib import Path
from typing import Any

STORE_DIR = Path(__file__).resolve().parent / "store"
STATE_DIR = STORE_DIR / "state"


class LoginStatus(str, enum.Enum):
    LOGGED_IN = "logged_in"
    NOT_LOGGED_IN = "not_logged_in"
    UNKNOWN = "unknown"


def store_dir() -> Path:
    return STORE_DIR


def profile_dir_for(site_key: str) -> Path:
    """每站一个独立 user_data_dir，cookie/存储互不串扰。"""
    return STORE_DIR / "profiles" / site_key / "user_data_dir"


def profile_meta(site_key: str) -> dict[str, Any]:
    """每站固定一套指纹参数，首建时取系统默认并落盘，之后每次复用同一值。

    指纹在同一 profile 内稳定不随机（同会话内抖动直接穿帮）。
    """
    meta_path = STORE_DIR / "profiles" / site_key / "profile.json"
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    meta = {
        "viewport": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
        "locale": "",   # 空 = 浏览器默认 = 系统语言
        "timezone": "",  # 空 = 浏览器默认 = 系统时区
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def check_login_signal(page: Any, signal: dict[str, Any] | None) -> bool | None:
    """按档案里的登录态信号匹配当前页：url_contains / selector / text 任一命中即已登录。

    返回 None 表示该站未配置登录信号（无从判断）。
    """
    if not signal:
        return None
    if signal.get("url_contains") and signal["url_contains"] in page.url:
        return True
    if signal.get("selector"):
        try:
            if page.locator(signal["selector"]).first.is_visible():
                return True
        except Exception:
            pass
    if signal.get("text"):
        try:
            if page.get_by_text(signal["text"], exact=False).first.is_visible():
                return True
        except Exception:
            pass
    return False


def record_login(site_key: str, status: str) -> None:
    state = {
        "site": site_key,
        "status": status,
        "last_login_at": datetime.now().isoformat(timespec="seconds"),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{site_key}.login.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_login_state(site_key: str) -> dict[str, Any] | None:
    path = STATE_DIR / f"{site_key}.login.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
