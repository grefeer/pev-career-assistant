from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "src" / "cli" / "data"
CHECKPOINT_DIR = ROOT_DIR / "checkpoints"


def load_env() -> None:
    # 固定从项目根目录读取 .env，避免从不同工作目录运行时找不到配置。
    load_dotenv(ROOT_DIR / ".env")
    literal_values = dotenv_values(ROOT_DIR / ".env", interpolate=False)
    for name in ("TENCENT_DOCS_TOKEN", "TEST_TENCENT_DOCS_TOKEN"):
        value = literal_values.get(name)
        if value:
            os.environ[name] = value


def read_text(path: Path) -> str:
    # 统一使用 UTF-8 读取文本，并去掉首尾空白。
    return path.read_text(encoding="utf-8").strip()


def load_jobs() -> list[dict[str, Any]]:
    # 当前版本直接从本地 JSON 读取岗位，方便学习和离线调试。
    return json.loads((DATA_DIR / "jobs.json").read_text(encoding="utf-8"))


def load_sample_resume() -> str:
    # 默认读取示例简历，后续也可以替换成真实简历内容。
    return read_text(DATA_DIR / "sample_resume.md")


def get_checkpoint_db_path() -> Path:
    # SQLite checkpoint 默认落在项目根目录下的 checkpoints 文件夹中。
    # 这样做有两个好处：
    # 1. 数据库文件和项目放在一起，便于演示与迁移
    # 2. 后续前端、命令行或 API 服务都能复用同一份 checkpoint 数据
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    return CHECKPOINT_DIR / "langgraph_checkpoints.sqlite"


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


def _get_windows_user_env(name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def get_api_key() -> str | None:
    # DeepSeek 官方 API 兼容 OpenAI 格式；优先读取用户环境变量中的 DeepSeek key。
    return (
        os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or _get_windows_user_env("DEEPSEEK_API_KEY")
        or _get_windows_user_env("OPENAI_API_KEY")
    )


def get_base_url() -> str:
    # 默认使用 DeepSeek 的 OpenAI-compatible endpoint，仍允许 OPENAI_BASE_URL 覆盖。
    return os.getenv("OPENAI_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)


def get_model_name(role_env: str | None = None, default: str = DEFAULT_DEEPSEEK_MODEL) -> str:
    # 第二版支持“按角色配置模型”：
    # 如果某个角色单独配置了模型，就优先使用；
    # 否则回退到全局 OPENAI_MODEL。
    if role_env and os.getenv(role_env):
        return os.getenv(role_env, default)
    return os.getenv("OPENAI_MODEL", default)
