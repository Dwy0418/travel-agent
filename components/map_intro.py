"""电影级 3D 开场（cinematic map intro）的 Streamlit 自定义组件封装。

为什么不用 st.components.v1.html：
- html() 是一次性内联，拿不到「播完 / 跳过 / 点开某地点」的回传值，主流程只能靠定时器猜；
- declare_component 走的是 iframe + postMessage 协议，组件里调用 Streamlit.setComponentValue
  就会触发一次 rerun，Python 侧拿到返回值，正好用来「跳过后进入行程详情」。

组件页是纯静态单文件（vendor 全部内联），不需要 npm 构建：
    components/map_intro_component/index.html   ← 由 work/build-single.mjs 生成

返回值为 None（还没交互）或 {"type": "intro:done" | "poi:open" | "poi:detail", "payload": {...}}。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

_COMPONENT_DIR = Path(__file__).parent / "map_intro_component"
_component = components.declare_component("map_intro", path=str(_COMPONENT_DIR))


def map_intro(
    *,
    payload: dict[str, Any] | None = None,
    key: str | None = None,
) -> dict[str, Any] | None:
    """挂载开场组件。

    payload 目前只做透传（组件内置了成都示例数据，真实行程数据建议用
    work/build-single.mjs 在构建期注入 window.CI_DATA，避免每次 rerun 都传 JSON）。
    """
    return _component(payload=payload or {}, key=key, default=None)
