"""全局配置：密钥只从环境变量 / .env / Streamlit Secrets 读取，源码中禁止出现任何密钥。

读取优先级：环境变量（本地 .env） > Streamlit Secrets（线上部署） > 代码默认值。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _from_streamlit_secrets(name: str) -> str:
    """线上兜底：Streamlit Community Cloud 把密钥放在 Secrets 面板（落盘成 secrets.toml）。

    没有 secrets 文件时直接返回空串，且不导入 streamlit —— 保证 cli / evaluate / selftest
    在没有 streamlit 或没有 Secrets 的环境下零额外开销、零报错。
    """
    try:
        candidates = (
            Path.home() / ".streamlit" / "secrets.toml",
            BASE_DIR / ".streamlit" / "secrets.toml",
        )
        if not any(path.exists() for path in candidates):
            return ""
        import streamlit as st  # 延迟导入：只有确认存在 secrets 文件才加载

        return str(st.secrets.get(name, "")).strip()
    except Exception:  # noqa: BLE001 - Secrets 缺失/格式错误时安静降级，由界面提示缺 Key
        return ""


def _cfg(name: str, default: str = "") -> str:
    """按「环境变量 -> Streamlit Secrets -> 默认值」的顺序取值。"""
    return _env(name) or _from_streamlit_secrets(name) or default


DEEPSEEK_API_KEY = _cfg("DEEPSEEK_API_KEY")
AMAP_KEY = _cfg("AMAP_KEY")
DEEPSEEK_BASE_URL = _cfg("DEEPSEEK_BASE_URL", "https://api.deepseek.com/beta").rstrip("/")
DEEPSEEK_MODEL = _cfg("DEEPSEEK_MODEL", "deepseek-chat")
# strict 工具模式需要 beta 端点；置 0 可关闭（兼容不支持 strict 的账号）
STRICT_TOOLS = _cfg("STRICT_TOOLS", "1") != "0"

# ---- 硬性网络规则 ----
HTTP_TIMEOUT = 10          # 所有接口 10 秒超时
HTTP_RETRIES = 2           # 失败重试 2 次：1s -> 2s
LLM_TIMEOUT = 90
LLM_RETRIES = 3            # LLM 重试 3 次：2s -> 4s -> 8s（服务端 5xx/限流较常见）
MAX_OUTPUT_TOKENS = int(_cfg("MAX_OUTPUT_TOKENS", "4096"))  # 多天行程 JSON 较长，2048 会被截断

# ---- 硬性流程规则 ----
MAX_ITERATIONS = 10        # 迭代上限，禁止死循环

# ---- 业务默认值（可被用户约束覆盖）----
DAILY_HOURS = 8.0          # 早 9 到晚 6
LUNCH_MINUTES = 60
DEFAULT_DWELL_HOURS = 2.0  # 每景点默认游览 2 小时
FOOD_PER_PERSON_PER_DAY = 150.0
HOTEL_PER_NIGHT = 200.0
WALK_KM_PER_ATTRACTION = 1.5
RAIN_PROB_THRESHOLD = 30   # 降水概率 >= 30% 视为不宜户外
MAX_ATTRACTIONS_PER_DAY = 3
ELDER_WALK_KM_LIMIT = 5.0


def require_keys(need_llm: bool = True) -> list[str]:
    """返回缺失的 Key 名称列表；不抛异常，由上层决定如何提示。"""
    missing: list[str] = []
    if need_llm and not DEEPSEEK_API_KEY:
        missing.append("DEEPSEEK_API_KEY")
    if not AMAP_KEY:
        missing.append("AMAP_KEY")
    return missing
