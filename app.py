"""Streamlit 界面（深色 / 毛玻璃 / 卡片式时间轴 UI 升级版）。

流程：左侧约束 -> st.status 上帝视角 -> 卡片式时间轴行程 -> 指标与行程单下载。
运行：streamlit run app.py

只负责显示：所有中英文对照来自 travel_agent.zh_labels，
原始参数 / 坐标 / 毫秒耗时写后台日志（logs/trace-detail.log），不上屏。
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st

from travel_agent import agent, config, zh_labels

st.set_page_config(page_title=zh_labels.PAGE_TITLE, page_icon="🧭", layout="wide")

KEY_LIMIT = zh_labels.KEY_TRACE_LIMIT

# ---------------------------------------------------------------------------
# 低版本兼容层：只做「有就用、没有就退」，绝不依赖新版独占特性
# 说明：不引入 st.pills / st.badge / st.fragment 等新 API。
# ---------------------------------------------------------------------------
def _call_with_fallback(func, *args, **kwargs):
    """按候选关键字逐个降级调用，全部失败就抛最后一个异常。"""
    variants = kwargs.pop("_variants", None) or [kwargs]
    last_error: Exception | None = None
    for variant in variants:
        call_kwargs = dict(variant)
        try:
            return func(*args, **call_kwargs)
        except TypeError as exc:  # 该版本不认识某个关键字
            last_error = exc
            if "decorator" in str(exc) or "unexpected" not in str(exc):
                # 非签名问题（例如上下文管理器用法错误）直接抛出，避免掩盖真实故障
                if "unexpected keyword" not in str(exc):
                    raise
    if last_error is not None:
        raise last_error
    return None


@contextmanager
def card():
    """卡片容器：优先 st.container(border=True)，老版本退回普通 container。"""
    container = st.container
    try:
        box = container(border=True)
    except TypeError:
        box = container()
    with box:
        yield box


def full_width_button(label: str, *, key: str | None = None, kind: str | None = None) -> bool:
    """满宽按钮：新版用 width="stretch"，老版用 use_container_width=True。"""
    kwargs: dict[str, Any] = {}
    if key:
        kwargs["key"] = key
    if kind:
        kwargs["type"] = kind
    try:
        return st.button(label, width="stretch", **kwargs)  # type: ignore[call-arg]
    except TypeError:
        return st.button(label, use_container_width=True, **kwargs)  # type: ignore[call-arg]


def full_width_download(label: str, **kwargs: Any) -> bool:
    """满宽下载按钮，同样做新旧参数降级。"""
    try:
        return st.download_button(label, width="stretch", **kwargs)
    except TypeError:
        return st.download_button(label, use_container_width=True, **kwargs)


def full_width_table(rows: Any, **kwargs: Any) -> None:
    """满宽表格：新版 width="stretch"，老版 use_container_width=True。"""
    try:
        st.dataframe(rows, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(rows, use_container_width=True, **kwargs)


def status_box(label: str, *, expanded: bool = True):
    """执行状态框：优先 st.status；老版本退回 spinner + 占位区。"""
    factory = getattr(st, "status", None)
    if callable(factory):
        try:
            return factory(label, expanded=expanded)
        except TypeError:
            pass
    return _FallbackStatus(label)


class _FallbackStatus:
    """st.status 的低版本替身：提供 update() 与上下文管理，行为尽量一致。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self._box = st.empty()
        self._box.info(f"⏳ {label}")

    def __enter__(self) -> "_FallbackStatus":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def update(self, *, label: str | None = None, state: str | None = None, expanded: bool | None = None) -> None:
        self.label = label or self.label
        icon = {"complete": "✅", "error": "❌", "running": "⏳"}.get(state or "running", "⏳")
        if state == "error":
            self._box.error(f"{icon} {self.label}")
        elif state == "complete":
            self._box.success(f"{icon} {self.label}")
        else:
            self._box.info(f"{icon} {self.label}")


# ---------------------------------------------------------------------------
# 控件选项：同行人带 Emoji，特殊偏好改多选
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 目的地三级联动数据：国家 -> 省份 -> 城市
# 说明：城市名与高德 geocode 可识别的行政区名保持一致，
# 避免联动选出的城市交给 Agent 后查不到 POI。
# ---------------------------------------------------------------------------
DESTINATION_TREE: dict[str, dict[str, list[str]]] = {
    "🇨🇳 中国": {
        "广东省": ["梅州市", "广州市", "深圳市", "珠海市", "佛山市", "惠州市", "汕头市", "东莞市", "中山市"],
        "四川省": ["成都市", "乐山市", "绵阳市", "阿坝州", "甘孜州", "宜宾市", "峨眉山市"],
        "北京市": ["北京市"],
        "上海市": ["上海市"],
        "浙江省": ["杭州市", "宁波市", "绍兴市", "嘉兴市", "温州市", "湖州市"],
        "江苏省": ["南京市", "苏州市", "无锡市", "扬州市", "常州市", "镇江市"],
        "云南省": ["昆明市", "大理市", "丽江市", "香格里拉市", "西双版纳州", "腾冲市"],
        "福建省": ["厦门市", "福州市", "泉州市", "漳州市", "武夷山市"],
        "陕西省": ["西安市", "宝鸡市", "咸阳市", "延安市"],
        "湖南省": ["长沙市", "张家界市", "凤凰县", "韶山市"],
        "广西壮族自治区": ["桂林市", "南宁市", "阳朔县", "北海市"],
        "海南省": ["海口市", "三亚市", "琼海市"],
        "山东省": ["青岛市", "济南市", "威海市", "烟台市", "淄博市"],
        "重庆市": ["重庆市"],
        "贵州省": ["贵阳市", "遵义市", "黔东南州", "安顺市"],
        "安徽省": ["合肥市", "黄山市", "芜湖市"],
        "江西省": ["南昌市", "景德镇市", "上饶市", "婺源县"],
        "河南省": ["郑州市", "洛阳市", "开封市"],
        "湖北省": ["武汉市", "宜昌市", "恩施州"],
        "山西省": ["太原市", "大同市", "平遥县"],
        "河北省": ["石家庄市", "承德市", "秦皇岛市"],
        "辽宁省": ["沈阳市", "大连市", "丹东市"],
        "吉林省": ["长春市", "延边州", "吉林市"],
        "黑龙江省": ["哈尔滨市", "牡丹江市", "漠河市"],
        "甘肃省": ["兰州市", "敦煌市", "张掖市", "嘉峪关市"],
        "青海省": ["西宁市", "海东市", "海西州"],
        "新疆维吾尔自治区": ["乌鲁木齐市", "喀什市", "吐鲁番市", "伊犁州"],
        "西藏自治区": ["拉萨市", "林芝市", "日喀则市"],
        "内蒙古自治区": ["呼和浩特市", "呼伦贝尔市", "鄂尔多斯市"],
        "天津市": ["天津市"],
        "宁夏回族自治区": ["银川市", "中卫市"],
    },
    # 预留位：后续要走出国门时，按同样三层结构补数据即可，联动与 payload 逻辑都不用改。
    "🌏 其他国家 / 地区（敬请期待）": {
        "🇯🇵 日本": ["东京", "大阪", "京都", "北海道"],
        "🇹🇭 泰国": ["曼谷", "清迈", "普吉岛"],
        "🇸🇬 新加坡": ["新加坡"],
    },
}

# ---------------------------------------------------------------------------
# 控件选项：同行人带 Emoji（10 类），特殊偏好改多选 + 自由补充
# ---------------------------------------------------------------------------
COMPANION_OPTIONS = [
    "👤 独自出行",
    "👩 ❤️ 👨 情侣/夫妻",
    "👴 带老人",
    "👶 带小孩",
    "👨 👩 👧 👦 亲子家庭",
    "👯 朋友结伴",
    "👨 💻 公司团建",
    "🐶 携带宠物",
    "🤰 孕妇",
    "♿ 需要无障碍设施",
]
# 提交给 Agent 的纯文本标签（顺序与 COMPANION_OPTIONS 一一对应）
COMPANION_LABELS = [
    "独自出行",
    "情侣/夫妻",
    "带老人",
    "带小孩",
    "亲子家庭",
    "朋友结伴",
    "公司团建",
    "携带宠物",
    "孕妇",
    "需要无障碍设施",
]

PREFERENCE_OPTIONS = [
    "🚶 少走路/休闲游",
    "🏛️ 室内为主",
    "📸 出片率高",
    "🍲 美食打卡",
    "🏔️ 户外自然",
    "🎢 特种兵拉练",
    "🛍️ 购物逛街",
    "🎭 文化历史",
]

PREFERENCE_LABELS = [p.split(" ", 1)[1] for p in PREFERENCE_OPTIONS]

TYPE_ICON = {
    "attraction": "📍",
    "food": "🍜",
    "hotel": "🏨",
    "transport": "🚗",
    "rest": "☕",
}

TYPE_LABEL = {
    "attraction": "游玩",
    "food": "用餐",
    "hotel": "住宿",
    "transport": "出行",
    "rest": "休息",
}

TRANSPORT_LABEL = {"driving": "打车/自驾", "walking": "步行", "transit": "公交地铁"}

PRESETS: list[dict[str, Any]] = [
    {
        "key": "meizhou",
        "icon": "👴",
        "title": "梅州客都悠闲 2 日游",
        "desc": "带老人慢节奏，围龙屋与客家博物馆",
        "payload": {
            "city": "梅州市",
            "days": 2,
            "people": 2,
            "budget": 3000,
            "companions": ["带老人"],
            "preferences": ["少走路/休闲游", "室内为主"],
            "request": "想去有当地文化特色的地方，节奏轻松些",
        },
    },
    {
        "key": "shanghai",
        "icon": "🏙️",
        "title": "上海室内看展打卡",
        "desc": "美术馆 + 展览，雨天也不怕",
        "payload": {
            "city": "上海市",
            "days": 2,
            "people": 2,
            "budget": 4500,
            "companions": ["情侣/夫妻"],
            "preferences": ["室内为主", "出片率高"],
            "request": "想看美术馆和展览，尽量少走路",
        },
    },
    {
        "key": "chuanxi",
        "icon": "🏔️",
        "title": "川西自驾特种兵 3 日",
        "desc": "高原风光与观景点，密度拉满",
        "payload": {
            "city": "成都市",
            "days": 3,
            "people": 2,
            "budget": 8000,
            "companions": ["朋友结伴"],
            "preferences": ["出片率高", "户外自然", "特种兵拉练"],
            "request": "以自驾为主，多安排高原风光与观景点",
        },
    },
]



# ---------------------------------------------------------------------------
# 全局 CSS（任务 5）：深灰蓝底 + 毛玻璃 + 渐变按钮 + 时间轴
# ---------------------------------------------------------------------------
GLOBAL_CSS = """
<style>
/* 1. 全局背景：深灰蓝 */
.stApp {
    background:
        radial-gradient(1200px 600px at 12% -10%, rgba(59,130,246,0.16), transparent 60%),
        radial-gradient(1000px 520px at 88% 0%, rgba(139,92,246,0.16), transparent 55%),
        #0F172A !important;
    color: #E2E8F0;
}

/* 2. 侧边栏：深色 + 右侧发光细边 */
section[data-testid="stSidebar"] {
    background: #1E293B !important;
    box-shadow: 2px 0 10px rgba(0, 0, 0, 0.5);
    border-right: 1px solid rgba(139, 92, 246, 0.22);
}
section[data-testid="stSidebar"] * { color: #E2E8F0; }
section[data-testid="stSidebar"] .sec-title {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1.2px;
    color: #93C5FD;
    margin: 14px 0 4px 0;
    text-transform: uppercase;
}
section[data-testid="stSidebar"] hr.sec-line {
    border: none;
    height: 1px;
    margin: 14px 0 2px 0;
    background: linear-gradient(90deg, rgba(139,92,246,0.65), rgba(59,130,246,0.05));
}

/* 3. 按钮：渐变 + 圆角 + Hover 上浮发光 */
.stButton > button,
.stDownloadButton > button {
    border-radius: 8px;
    border: 1px solid rgba(148, 163, 184, 0.28);
    background: rgba(30, 41, 59, 0.75);
    color: #E2E8F0;
    font-weight: 600;
    transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
.stButton > button:hover,
.stDownloadButton > button:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(139, 92, 246, 0.4);
    background: rgba(51, 65, 85, 0.9);
    color: #FFFFFF;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #3B82F6, #8B5CF6);
    border: none;
    border-radius: 8px;
    color: #FFFFFF;
    font-size: 15px;
    font-weight: 700;
    padding: 0.6rem 1rem;
    box-shadow: 0 6px 18px rgba(59, 130, 246, 0.28);
}
.stButton > button[kind="primary"]:hover {
    transform: translateY(-2px);
    box-shadow: 0 4px 12px rgba(139, 92, 246, 0.4);
    background: linear-gradient(90deg, #4F8FF7, #9B6CF8);
}

/* 4. 输入框：半透明深色 + 微弱边框 */
div[data-baseweb="input"],
div[data-baseweb="textarea"],
div[data-baseweb="select"] > div,
div[data-testid="stNumberInputContainer"] {
    background: rgba(15, 23, 42, 0.55) !important;
    border: 1px solid rgba(148, 163, 184, 0.25) !important;
    border-radius: 10px !important;
    box-shadow: none !important;
}
div[data-baseweb="input"] input,
div[data-baseweb="textarea"] textarea,
div[data-baseweb="select"] input,
div[data-testid="stNumberInputContainer"] input {
    background: transparent !important;
    color: #E2E8F0 !important;
}
div[data-baseweb="input"]:focus-within,
div[data-baseweb="textarea"]:focus-within,
div[data-baseweb="select"] > div:focus-within {
    border-color: rgba(139, 92, 246, 0.75) !important;
    box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.18) !important;
}

/* 5. 标题层级：行高与字重 */
h1, h2, h3, h4 {
    color: #F1F5F9 !important;
    line-height: 1.28 !important;
    letter-spacing: 0.2px;
}
h1 { font-weight: 800 !important; margin-bottom: 0.2rem !important; }
h2 { font-weight: 700 !important; margin-top: 1.1rem !important; }
h3 { font-weight: 650 !important; color: #CBD5E1 !important; }
p, li, label, span { color: #CBD5E1; }

/* 主内容区：去掉默认白色边框与顶栏色块，形成悬浮卡片感 */
div[data-testid="stHeader"] { background: transparent !important; }
div[data-testid="stDecoration"] { display: none !important; }
div[data-testid="stToolbar"] { right: 1rem; }
section.main > div.block-container,
div[data-testid="stAppViewContainer"] .block-container {
    padding-top: 2.4rem;
    padding-bottom: 3rem;
    max-width: 1320px;
}
section.main div[data-testid="stVerticalBlockBorderWrapper"] > div:first-child {
    border: none !important;
}

/* 毛玻璃卡片容器 */
div[data-testid="stVerticalBlockBorderWrapper"] {
    background: rgba(30, 41, 59, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.18) !important;
    border-radius: 16px;
    backdrop-filter: blur(10px);
    box-shadow: 0 10px 30px rgba(2, 6, 23, 0.45);
}
div[data-testid="stMetric"] {
    background: rgba(30, 41, 59, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 14px;
    padding: 10px 14px;
}
div[data-testid="stMetricValue"] { color: #F8FAFC !important; }

/* 首屏 Empty State —— 承担品牌主视觉（已取代全局大标题） */
.hero {
    text-align: center;
    padding: 72px 12px 30px 12px;
}
.hero-icon {
    font-size: 64px;
    line-height: 1;
    filter: drop-shadow(0 8px 22px rgba(139, 92, 246, 0.45));
}
.hero-title {
    font-size: 48px;
    font-weight: 900;
    letter-spacing: 1px;
    line-height: 1.18;
    margin: 0;
    background: linear-gradient(96deg, #DBEAFE 0%, #93C5FD 38%, #C4B5FD 72%, #F0ABFC 100%);
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    filter: drop-shadow(0 6px 20px rgba(139, 92, 246, 0.28));
}
.hero-sub {
    color: #94A3B8;
    font-size: 14.5px;
    line-height: 1.85;
    max-width: 660px;
    margin: 14px auto 0 auto;
}
.preset-icon { font-size: 30px; line-height: 1.2; }
.preset-title {
    font-size: 16px;
    font-weight: 700;
    color: #E2E8F0;
    margin: 2px 0 2px 0;
}

/* 行程卡片头部 */
.day-head {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    padding-bottom: 6px;
}
.day-badge {
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 0.6px;
    padding: 3px 10px;
    border-radius: 999px;
    color: #FFFFFF;
    background: linear-gradient(90deg, #3B82F6, #8B5CF6);
    box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35);
}
.day-icon { font-size: 18px; }
.day-theme { font-size: 16px; font-weight: 700; color: #E2E8F0; }
.day-date { font-size: 12px; color: #94A3B8; margin-left: auto; }

/* 时间轴 */
.tl-wrap {
    position: relative;
    margin: 10px 0 2px 0;
    padding-left: 24px;
}
.tl-wrap::before {
    content: "";
    position: absolute;
    left: 6px;
    top: 6px;
    bottom: 10px;
    width: 2px;
    border-radius: 2px;
    background: linear-gradient(180deg, #3B82F6, #8B5CF6);
    opacity: 0.55;
}
.tl-item { position: relative; margin: 0 0 16px 0; }
.tl-item::before {
    content: "";
    position: absolute;
    left: -22px;
    top: 5px;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #0F172A;
    border: 2px solid #8B5CF6;
    box-shadow: 0 0 8px rgba(139, 92, 246, 0.7);
}
.tl-time {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.4px;
    color: #93C5FD;
}
.tl-name { font-size: 15px; font-weight: 650; color: #E2E8F0; margin: 2px 0; }
.tl-note { font-size: 13px; color: #94A3B8; line-height: 1.55; }
.tl-foot { margin-top: 4px; }
.tl-meta { font-size: 12px; color: #64748B; margin-right: 8px; }
.tl-nav {
    display: inline-block;
    padding: 1px 9px;
    border-radius: 999px;
    font-size: 12px;
    text-decoration: none !important;
    color: #93C5FD !important;
    background: rgba(59, 130, 246, 0.16);
    border: 1px solid rgba(59, 130, 246, 0.35);
    transition: background 0.18s ease, transform 0.18s ease;
}
.tl-nav:hover {
    background: rgba(139, 92, 246, 0.28);
    transform: translateY(-1px);
}

/* ================= C 端化：看板 / AI 气泡 / 提示标签 / 游记节点 ================= */

/* 顶部数据看板 */
.dash {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    margin: 6px 0 18px 0;
}
.dash-card {
    flex: 1 1 0;
    min-width: 190px;
    background: #1E293B;
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 16px;
    padding: 14px 18px;
    box-shadow: 0 8px 24px rgba(2, 6, 23, 0.4);
}
.dash-label {
    font-size: 12px;
    color: #94A3B8;
    letter-spacing: 0.6px;
    margin-bottom: 4px;
}
.dash-value {
    font-size: 24px;
    font-weight: 800;
    color: #F8FAFC;
    line-height: 1.2;
}
.dash-sub { font-size: 12px; color: #94A3B8; margin-top: 4px; }
.dash-amber .dash-value { color: #FBBF24; }

/* 预算进度条 */
.bar-wrap {
    height: 12px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.18);
    overflow: hidden;
    margin: 10px 0 8px 0;
}
.bar-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #22D3EE, #3B82F6);
    box-shadow: 0 0 12px rgba(59, 130, 246, 0.55);
}
.bar-fill.warn { background: linear-gradient(90deg, #FBBF24, #F97316); }
.bar-fill.over { background: linear-gradient(90deg, #F97316, #EF4444); }
.bill-row {
    display: flex;
    justify-content: space-between;
    font-size: 13px;
    color: #CBD5E1;
    padding: 5px 0;
    border-bottom: 1px dashed rgba(148, 163, 184, 0.16);
}
.bill-row:last-child { border-bottom: none; }
.bill-row span:last-child { color: #E2E8F0; font-weight: 600; }

/* AI 伴游对话气泡 */
.ai-bubble {
    display: flex;
    gap: 12px;
    align-items: flex-start;
    background: linear-gradient(135deg, rgba(59,130,246,0.16), rgba(139,92,246,0.16));
    border: 1px solid rgba(139, 92, 246, 0.35);
    border-radius: 18px;
    padding: 16px 20px;
    margin: 4px 0 20px 0;
    box-shadow: 0 10px 30px rgba(2, 6, 23, 0.42);
}
.ai-avatar {
    width: 38px; height: 38px;
    flex: 0 0 38px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 19px;
    background: linear-gradient(135deg, #3B82F6, #8B5CF6);
    box-shadow: 0 4px 12px rgba(99, 102, 241, 0.45);
}
.ai-name { font-size: 12px; color: #93C5FD; margin-bottom: 3px; letter-spacing: 0.5px; }
.ai-text { font-size: 14.5px; color: #E2E8F0; line-height: 1.7; }

/* 每日卡片头 */
.day-title {
    font-size: 20px;
    font-weight: 800;
    color: #F8FAFC;
    letter-spacing: 0.3px;
}
.day-theme2 { font-size: 13px; color: #94A3B8; margin-top: 2px; }
.day-weather {
    margin-left: auto;
    font-size: 13px;
    color: #FDE68A;
    background: rgba(251, 191, 36, 0.12);
    border: 1px solid rgba(251, 191, 36, 0.3);
    border-radius: 999px;
    padding: 3px 11px;
}

/* 游记时间轴节点 */
.nd {
    position: relative;
    display: flex;
    gap: 12px;
    padding: 0 0 18px 0;
}
.nd::before {
    content: "";
    position: absolute;
    left: 5px;
    top: 22px;
    bottom: 0;
    width: 2px;
    background: linear-gradient(180deg, rgba(59,130,246,0.7), rgba(139,92,246,0.15));
}
.nd:last-child::before { display: none; }
.nd-dot {
    flex: 0 0 12px;
    width: 12px; height: 12px;
    margin-top: 6px;
    border-radius: 50%;
    background: #0F172A;
    border: 2px solid #8B5CF6;
    box-shadow: 0 0 10px rgba(139, 92, 246, 0.8);
}
.nd-main { flex: 1; }
.nd-time {
    font-size: 12px;
    font-weight: 700;
    color: #93C5FD;
    letter-spacing: 0.4px;
}
.nd-name {
    font-size: 15.5px;
    font-weight: 700;
    color: #F1F5F9;
    margin: 2px 0 4px 0;
}
.nd-line { font-size: 13px; color: #94A3B8; margin: 2px 0; }
.nd-note { font-size: 13px; color: #CBD5E1; margin: 4px 0 0 0; line-height: 1.6; }
.nd-foot { margin-top: 7px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 温馨提示卡片：柔和语气 + 圆角标签 */
.tip-card {
    display: block;
    font-size: 13.5px;
    color: #E2E8F0;
    background: rgba(251, 191, 36, 0.08);
    border: 1px solid rgba(251, 191, 36, 0.22);
    border-radius: 14px;
    padding: 11px 15px;
    margin: 8px 0;
    line-height: 1.78;
}
.tip-card .tip-soft { color: #FDE68A; }

/* 柔和提示条（自动重排说明），不出现报错感 */
.soft-note {
    font-size: 13px;
    color: #BBF7D0;
    background: rgba(34, 197, 94, 0.10);
    border: 1px solid rgba(34, 197, 94, 0.22);
    border-radius: 12px;
    padding: 10px 14px;
    margin: 8px 0;
    line-height: 1.7;
}

/* 行程一览表：暗色护眼，去掉生硬边框 */
.itn-wrap {
    border-radius: 16px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.16);
    background: #1E293B;
    margin: 6px 0 14px 0;
}
.itn-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.itn-table thead th {
    background: rgba(139, 92, 246, 0.14);
    color: #C4B5FD;
    font-weight: 700;
    text-align: left;
    padding: 11px 13px;
    font-size: 12.5px;
    letter-spacing: 0.3px;
    border: none;
    white-space: nowrap;
}
.itn-table tbody td {
    padding: 11px 13px;
    color: #E2E8F0;
    border: none;
    border-top: 1px solid rgba(148, 163, 184, 0.10);
    vertical-align: top;
    line-height: 1.62;
}
.itn-table tbody tr:nth-child(even) td { background: rgba(148, 163, 184, 0.05); }
.itn-table td.itn-time { color: #93C5FD; font-weight: 700; white-space: nowrap; }
.itn-table td.itn-cost { color: #FCD34D; font-weight: 600; white-space: nowrap; }
.itn-table td.itn-tip { color: #CBD5E1; font-size: 12.5px; }
.itn-day-row td {
    background: rgba(139, 92, 246, 0.10) !important;
    color: #DDD6FE !important;
    font-weight: 700;
    font-size: 13px;
}
/* 分组分隔线：统一成低调细线，避免亮蓝生硬 */
.sec-line {
    border: none;
    border-top: 1px solid rgba(148, 163, 184, 0.18);
    margin: 2px 0 8px 0;
    height: 0;
}

/* 侧边栏加宽：多选项两列排布时留足呼吸空间 */
section[data-testid="stSidebar"] {
    min-width: 352px;
}

/* 多选分组的字段名（checkbox 网格自带标签，不依赖 placeholder） */
.field-label {
    font-size: 13.5px;
    font-weight: 700;
    color: #CBD5E1;
    margin: 10px 0 4px 0;
    letter-spacing: 0.2px;
}

/* checkbox 选项：让它看起来像可点的小标签，而不是开发控件 */
section[data-testid="stSidebar"] [data-testid="stCheckbox"] label {
    background: rgba(148, 163, 184, 0.07);
    border: 1px solid rgba(148, 163, 184, 0.14);
    border-radius: 9px;
    padding: 5px 9px;
    transition: background 0.15s ease, border-color 0.15s ease;
}
section[data-testid="stSidebar"] [data-testid="stCheckbox"] label:hover {
    background: rgba(139, 92, 246, 0.14);
    border-color: rgba(139, 92, 246, 0.38);
}
section[data-testid="stSidebar"] [data-testid="stCheckbox"] p {
    font-size: 12.5px;
    line-height: 1.35;
}

/* ==========================================================================
   精装修 V2：层次 / 留白 / 卡片质感 / 色彩焦点 / 光晕
   仅做视觉覆盖，放在最后，不改动任何下拉框级联或业务逻辑。
   ========================================================================== */

/* ---------- 维度一：背景与层次 ---------- */
.stApp {
    background:
        radial-gradient(1100px 560px at 14% -8%, rgba(59, 130, 246, 0.14), transparent 62%),
        radial-gradient(900px 480px at 86% -2%, rgba(139, 92, 246, 0.14), transparent 58%),
        #0B1120 !important;
}

/* 侧边栏：悬浮感（右侧柔和投影，而非生硬切一刀） */
section[data-testid="stSidebar"] {
    background: #101A2E !important;
    border-right: none !important;
    box-shadow: 14px 0 36px -14px rgba(2, 6, 23, 0.90),
                1px 0 0 rgba(148, 163, 184, 0.08) !important;
    backdrop-filter: blur(16px);
}

/* 输入框：半透明磨砂 + 极细暗边，hover / focus 才发光 */
div[data-baseweb="input"],
div[data-baseweb="textarea"],
div[data-baseweb="select"] > div,
div[data-testid="stNumberInputContainer"] {
    background: rgba(255, 255, 255, 0.05) !important;
    border: 1px solid rgba(148, 163, 184, 0.16) !important;
    border-radius: 10px !important;
    box-shadow: none !important;
    transition: border-color 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
div[data-baseweb="input"]:hover,
div[data-baseweb="textarea"]:hover,
div[data-baseweb="select"] > div:hover,
div[data-testid="stNumberInputContainer"]:hover {
    background: rgba(255, 255, 255, 0.075) !important;
    border-color: rgba(148, 163, 184, 0.30) !important;
}
div[data-baseweb="input"]:focus-within,
div[data-baseweb="textarea"]:focus-within,
div[data-baseweb="select"] > div:focus-within,
div[data-testid="stNumberInputContainer"]:focus-within,
div[data-testid="stNumberInputContainer"]:has(input:focus) {
    background: rgba(255, 255, 255, 0.08) !important;
    border-color: #3B82F6 !important;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.22) !important;
}

/* ---------- 维度二：排版与留白 ---------- */
.hero {
    position: relative;
    padding: 96px 12px 46px 12px;
    margin-bottom: 10px;
}
.hero-title {
    font-size: 50px;
    margin: 0 0 8px 0;
}
.hero-sub {
    font-size: 13.5px;
    color: #7C8CA3;
    opacity: 0.92;
    line-height: 1.9;
    margin: 16px auto 0 auto;
}

/* 侧边栏分组标题：放大加粗 + 下方留白，与控件分离 */
section[data-testid="stSidebar"] .sec-title {
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 1.3px;
    color: #BFDBFE;
    margin: 28px 0 12px 0;
    text-transform: none;
}
section[data-testid="stSidebar"] hr.sec-line {
    margin: 10px 0 6px 0;
}
section[data-testid="stSidebar"] .field-label {
    margin: 16px 0 6px 0;
}
/* 侧边栏小字提示（同行人下方等）整齐对齐 */
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
section[data-testid="stSidebar"] small {
    display: block;
    line-height: 1.75;
    margin-top: 6px;
    padding-left: 2px;
    color: #8A9AB0;
}

/* ---------- 维度三：示例卡片质感 ---------- */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) {
    background: #1E293B !important;
    border: 1px solid rgba(148, 163, 184, 0.10) !important;
    border-radius: 12px !important;
    padding: 18px 16px 14px 16px;
    box-shadow: 0 6px 18px rgba(2, 6, 23, 0.45);
    transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.3, 1),
                box-shadow 0.22s ease, border-color 0.22s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title):hover {
    transform: translateY(-4px);
    border-color: rgba(139, 92, 246, 0.42) !important;
    box-shadow: 0 14px 34px rgba(59, 130, 246, 0.26),
                0 0 0 1px rgba(139, 92, 246, 0.20);
}
.preset-icon {
    font-size: 32px;
    filter: drop-shadow(0 6px 16px rgba(139, 92, 246, 0.35));
}
.preset-title {
    font-size: 16.5px;
    margin: 8px 0 4px 0;
    color: #F1F5F9;
}

/* 卡片内按钮 → 幽灵按钮（透明底 + 主色描边），hover 反色填充 */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) .stButton > button {
    background: transparent !important;
    border: 1px solid rgba(139, 92, 246, 0.55) !important;
    border-radius: 9px;
    color: #C4B5FD !important;
    font-weight: 600;
    box-shadow: none;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) .stButton > button:hover {
    background: linear-gradient(90deg, #3B82F6, #8B5CF6) !important;
    border-color: transparent !important;
    color: #FFFFFF !important;
    transform: translateY(-1px);
    box-shadow: 0 6px 18px rgba(99, 102, 241, 0.45);
}

/* ---------- 维度四：色彩焦点强化 ---------- */
.stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #3B82F6 0%, #6366F1 52%, #8B5CF6 100%) !important;
    border: none !important;
    border-radius: 11px !important;
    color: #FFFFFF !important;
    font-size: 15.5px;
    font-weight: 800;
    letter-spacing: 0.6px;
    padding: 0.72rem 1rem;
    box-shadow: 0 4px 15px rgba(59, 130, 246, 0.40);
    transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
}
.stButton > button[kind="primary"]:hover {
    transform: translateY(-2px);
    filter: brightness(1.08);
    box-shadow: 0 8px 26px rgba(99, 102, 241, 0.52);
}

/* ---------- 维度五：标题光晕 ---------- */
.hero::before {
    content: "";
    position: absolute;
    left: 50%;
    top: 14px;
    width: 640px;
    height: 330px;
    transform: translateX(-50%);
    background: radial-gradient(closest-side,
                rgba(99, 102, 241, 0.30),
                rgba(59, 130, 246, 0.12) 55%,
                transparent 78%);
    filter: blur(8px);
    pointer-events: none;
    z-index: 0;
}
.hero-title, .hero-sub { position: relative; z-index: 1; }

/* ==========================================================================
   V3 终极改造：去压迫感 / 放诱惑力 / 收敛光污染 / 引导 CTA
   纯视觉覆盖，放在最后；不动任何级联、表单状态与业务逻辑。
   ========================================================================== */

/* ---------- 三、收敛光污染：统一 #0F172A，去掉上下色差 ---------- */
.stApp {
    background: #0F172A !important;
}
/* 干掉 V2 的紫色光晕 */
.hero::before {
    content: none !important;
    display: none !important;
    background: none !important;
    filter: none !important;
}
/* 标题回归极简：纯白 / 亮银 加粗，无渐变无发光 */
.hero-title {
    background: none !important;
    -webkit-background-clip: border-box !important;
    background-clip: border-box !important;
    -webkit-text-fill-color: #FFFFFF !important;
    color: #FFFFFF !important;
    filter: none !important;
    text-shadow: none !important;
    font-size: 44px;
    font-weight: 800;
    letter-spacing: 0.5px;
    margin: 0 0 10px 0;
}
.hero-sub {
    color: #8C9AAF !important;
    opacity: 1;
    font-size: 14px;
    font-weight: 400;
}

/* ---------- 一、祛除压迫感：重塑输入框（去掉土黄/深色硬边框） ---------- */
/* 覆盖所有可能的容器级选择器，避免任何版本留下硬边框 */
div[data-baseweb="input"],
div[data-baseweb="select"] > div,
div[data-baseweb="textarea"],
div[data-baseweb="base-input"],
div[data-testid="stNumberInputContainer"],
div[data-testid="stDateInput"] > div,
div[data-testid="stTextInput"] > div,
div[data-testid="stTextArea"] > div,
div[data-testid="stSelectbox"] > div {
    background: rgba(255, 255, 255, 0.05) !important;
    border: 1px solid transparent !important;
    border-radius: 8px !important;
    box-shadow: none !important;
    outline: none !important;
    transition: border-color 0.2s ease, box-shadow 0.2s ease, background 0.2s ease;
}
/* 内层 input 也清掉边框 / 背景，避免二次描边 */
div[data-baseweb="input"] input,
div[data-baseweb="base-input"] input,
div[data-baseweb="select"] input,
div[data-baseweb="textarea"] textarea,
div[data-testid="stNumberInputContainer"] input,
div[data-testid="stDateInput"] input {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    outline: none !important;
    color: #E8EEF7 !important;
}
/* 悬停：极轻微提亮，不出现强边框 */
div[data-baseweb="input"]:hover,
div[data-baseweb="select"] > div:hover,
div[data-baseweb="textarea"]:hover,
div[data-testid="stNumberInputContainer"]:hover,
div[data-testid="stDateInput"] > div:hover,
div[data-testid="stTextInput"] > div:hover {
    background: rgba(255, 255, 255, 0.075) !important;
}
/* 聚焦：只在这一刻出现柔和亮蓝边框 */
div[data-baseweb="input"]:focus-within,
div[data-baseweb="select"] > div:focus-within,
div[data-baseweb="textarea"]:focus-within,
div[data-baseweb="base-input"]:focus-within,
div[data-testid="stNumberInputContainer"]:focus-within,
div[data-testid="stNumberInputContainer"]:has(input:focus),
div[data-testid="stDateInput"] > div:focus-within,
div[data-testid="stTextInput"] > div:focus-within,
div[data-testid="stTextArea"] > div:focus-within,
div[data-testid="stSelectbox"] > div:focus-within {
    background: rgba(255, 255, 255, 0.08) !important;
    border-color: rgba(59, 130, 246, 0.85) !important;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.18) !important;
}
/* 关闭 Streamlit / baseweb 默认的 focus 高亮条（常见的土黄/深色描边来源） */
div[data-baseweb="input"] > div,
div[data-baseweb="select"] > div > div,
div[data-baseweb="textarea"] > div {
    border: none !important;
    box-shadow: none !important;
    background: transparent !important;
}
input:focus, textarea:focus, select:focus {
    outline: none !important;
    box-shadow: none !important;
}
/* 日期选择器弹层保持深色协调 */
div[data-baseweb="calendar"], div[data-baseweb="calendar"] * {
    border-color: rgba(148, 163, 184, 0.16) !important;
}

/* 侧边栏：与主区轻微色差 + 一条极弱右分割线 */
section[data-testid="stSidebar"] {
    background: #111C31 !important;
    border-right: none !important;
    box-shadow: 1px 0 0 rgba(148, 163, 184, 0.10) !important;
    backdrop-filter: none !important;
}

/* ---------- 二、增强诱惑力：卡片与幽灵按钮 ---------- */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) {
    background: #1E293B !important;
    border: none !important;
    border-radius: 12px !important;
    padding: 20px 18px 16px 18px;
    box-shadow: 0 4px 14px rgba(2, 6, 23, 0.35);
    transition: transform 0.22s cubic-bezier(0.2, 0.8, 0.3, 1),
                box-shadow 0.22s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title):hover {
    transform: translateY(-5px);
    box-shadow: 0 16px 36px rgba(2, 6, 23, 0.55),
                0 0 0 1px rgba(59, 130, 246, 0.30);
}
.preset-icon {
    font-size: 32px;
    filter: none;
}
.preset-title {
    font-size: 16.5px;
    font-weight: 700;
    color: #F3F7FC;
    margin: 8px 0 4px 0;
}
/* 卡片内按钮：蓝色幽灵按钮（透明底 + 亮蓝边框与文字），悬停变亮 */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) .stButton > button {
    background: transparent !important;
    border: 1px solid rgba(59, 130, 246, 0.75) !important;
    border-radius: 8px !important;
    color: #60A5FA !important;
    font-weight: 600;
    box-shadow: none !important;
    transition: background 0.18s ease, color 0.18s ease,
                border-color 0.18s ease, box-shadow 0.18s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.preset-title) .stButton > button:hover {
    background: rgba(59, 130, 246, 0.16) !important;
    border-color: #60A5FA !important;
    color: #BFDBFE !important;
    box-shadow: 0 0 12px rgba(59, 130, 246, 0.35) !important;
    transform: none;
}

/* ---------- 四、引导下一步行动：CTA 固定侧栏底部 + 呼吸光晕 ---------- */
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
    padding-bottom: 104px;
}
/* 侧栏里唯一的 primary 按钮就是「开始规划」 */
section[data-testid="stSidebar"] .stButton:has(> button[kind="primary"]) {
    position: sticky;
    bottom: 10px;
    z-index: 60;
    margin-top: 12px;
    padding: 8px 0 4px 0;
    background: linear-gradient(180deg, rgba(17, 28, 49, 0) 0%, #111C31 42%);
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"],
.stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #3B82F6 0%, #6366F1 55%, #8B5CF6 100%) !important;
    border: none !important;
    border-radius: 10px !important;
    color: #FFFFFF !important;
    font-size: 15.5px;
    font-weight: 800;
    letter-spacing: 0.6px;
    padding: 0.72rem 1rem;
    box-shadow: 0 0 15px rgba(59, 130, 246, 0.50),
                0 6px 18px rgba(59, 130, 246, 0.28);
    animation: ctaBreath 2.8s ease-in-out infinite;
}
@keyframes ctaBreath {
    0%, 100% { box-shadow: 0 0 13px rgba(59, 130, 246, 0.42), 0 6px 18px rgba(59, 130, 246, 0.24); }
    50%      { box-shadow: 0 0 22px rgba(59, 130, 246, 0.68), 0 8px 24px rgba(99, 102, 241, 0.34); }
}
section[data-testid="stSidebar"] .stButton > button[kind="primary"]:hover,
.stButton > button[kind="primary"]:hover {
    animation: none;
    transform: translateY(-2px);
    filter: brightness(1.08);
    box-shadow: 0 0 26px rgba(59, 130, 246, 0.75),
                0 10px 28px rgba(99, 102, 241, 0.42);
}

</style>
"""


def inject_css() -> None:
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 日志与中文映射（保持原逻辑）
# ---------------------------------------------------------------------------
def setup_detail_log() -> None:
    """技术明细写入后台日志文件；写不了就静默降级，不影响页面。"""
    logger = logging.getLogger("travel_agent.detail")
    if logger.handlers:
        return
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(exist_ok=True)
        handler = logging.FileHandler(log_dir / "trace-detail.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except OSError:
        logger.addHandler(logging.NullHandler())


def key_trace_lines(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["line"]) for entry in entries if zh_labels.is_key_trace(entry) and entry.get("line")]


def tech_trace_lines(entries: list[dict[str, Any]]) -> list[str]:
    return [zh_labels.describe_technical(entry) for entry in entries if zh_labels.is_tech_trace(entry)]


def render_key_trace(entries: list[dict[str, Any]], *, expanded: bool = False) -> None:
    """已按 C 端化要求下线：执行过程与技术明细不再上屏。

    保留函数签名只为兼容既有调用与测试，内部不做任何渲染。
    """
    _ = (entries, expanded)
    return None


# ---------------------------------------------------------------------------
# 任务 2：示例卡片与表单预填
# ---------------------------------------------------------------------------
def _companion_option(label: str) -> str | None:
    """纯文本人群标签 -> 带 Emoji 的选项（按索引精确对应，避免包含误判）。"""
    text = str(label or "").strip()
    for idx, plain in enumerate(COMPANION_LABELS):
        if plain == text:
            return COMPANION_OPTIONS[idx]
    for idx, plain in enumerate(COMPANION_LABELS):
        if plain in text:
            return COMPANION_OPTIONS[idx]
    return None


def _preference_option(label: str) -> str | None:
    """纯文本偏好标签 -> 带 Emoji 的选项。"""
    text = str(label or "").strip()
    for idx, plain in enumerate(PREFERENCE_LABELS):
        if plain == text or plain in text or text in plain:
            return PREFERENCE_OPTIONS[idx]
    return None


def _locate_city(city: str) -> tuple[str, str]:
    """按城市名反查 (国家, 省份)，用于示例卡片回填三级联动。"""
    target = str(city or "").strip()
    for country, provinces in DESTINATION_TREE.items():
        for province, cities in provinces.items():
            if target in cities:
                return country, province
    first_country = next(iter(DESTINATION_TREE))
    first_province = next(iter(DESTINATION_TREE[first_country]))
    return first_country, first_province


def _split_preferences(text: str) -> tuple[list[str], str]:
    """把「少走路/休闲游、室内为主；想吃正宗螺蛳粉」拆成 (选项列表, 自由补充)。"""
    raw = str(text or "")
    extra = ""
    if "；" in raw:
        raw, extra = raw.split("；", 1)
    picked: list[str] = []
    for part in [p for p in raw.split("、") if p.strip()]:
        opt = _preference_option(part)
        if opt and opt not in picked:
            picked.append(opt)
    return picked, extra.strip()


def _payload_to_state(payload: dict[str, Any]) -> None:
    """把一份 payload 映射回各控件的 session_state key。

    只能在控件实例化之前调用（run 顶部），否则 Streamlit 会因「修改已实例化
    控件的值」抛 StreamlitAPIException。
    """
    city = payload.get("city", CONSTRAINT_DEFAULTS["city"])
    country = payload.get("country")
    province = payload.get("province")
    if not (country and province):
        country, province = _locate_city(city)
    st.session_state["f_country"] = country
    st.session_state["f_province"] = province
    st.session_state["f_city"] = city

    st.session_state["f_days"] = int(payload.get("days") or CONSTRAINT_DEFAULTS["days"])
    st.session_state["f_people"] = int(payload.get("people") or CONSTRAINT_DEFAULTS["people"])
    st.session_state["f_budget"] = int(float(payload.get("budget") or CONSTRAINT_DEFAULTS["budget"]))

    prefs = payload.get("preferences_list")
    note = payload.get("preferences_note")
    if prefs is None:
        prefs, parsed = _split_preferences(str(payload.get("preferences") or ""))
        note = note if note is not None else parsed
    st.session_state["f_prefs"] = [o for o in (_preference_option(p) for p in (prefs or [])) if o]
    st.session_state["f_pref_note"] = str(note or "")

    st.session_state["f_companions"] = [
        opt for opt in (_companion_option(tag) for tag in (payload.get("companions") or [])) if opt
    ]
    st.session_state["f_request"] = payload.get("request", CONSTRAINT_DEFAULTS["request"])
    start = payload.get("start_date")
    span = max(0, int(payload.get("days") or CONSTRAINT_DEFAULTS["days"]) - 1)
    if start:
        start_day = date.fromisoformat(str(start))
        st.session_state["f_start"] = start_day
        # 回程日期：优先用显式传入的 end，否则按天数推算
        st.session_state["f_end"] = payload.get("end") or (start_day + timedelta(days=span))


def apply_pending_constraints() -> None:
    """应用「调整条件」提交的新参数（必须在控件实例化前执行）。"""
    pending = st.session_state.pop("_pending_constraints", None)
    if pending:
        _payload_to_state(pending)


def apply_pending_preset() -> None:
    """示例卡片点击后，先把参数写进 session_state，再让控件按新值渲染。"""
    preset = st.session_state.pop("_pending_preset", None)
    if not preset:
        return
    _payload_to_state(preset["payload"])
    st.session_state["_preset_hint"] = preset["title"]


def render_empty_state() -> None:
    """首屏视觉中心：Agent 身份 + 3 张示例卡片（取代全局大标题）。"""
    st.markdown(
        "<div class='hero'>"
        "<div class='hero-title'>✈️ 旅游规划助手</div>"
        "<div class='hero-sub'>我是您的贴心旅行管家，请告诉我您的需求，"
        "或者直接选择下方的一个示例开启旅程吧！</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    cols = st.columns(3, gap="medium")
    for col, preset in zip(cols, PRESETS):
        with col:
            with card():
                st.markdown(f"<div class='preset-icon'>{preset['icon']}</div>", unsafe_allow_html=True)
                st.markdown(f"<div class='preset-title'>{escape(preset['title'])}</div>", unsafe_allow_html=True)
                st.caption(preset["desc"])
                if full_width_button("套用这个旅程", key=f"preset_{preset['key']}"):
                    st.session_state["_pending_preset"] = preset
                    st.rerun()


# ---------------------------------------------------------------------------
# 任务 1：侧边栏（分组 + 控件升级 + 主按钮）
# ---------------------------------------------------------------------------
def seed_state(key: str, default: Any) -> None:
    """控件默认值只写 session_state：避免「default + key」双写引发告警，
    也让示例卡片能真正改写控件初值。"""
    if key not in st.session_state:
        st.session_state[key] = default


def checkbox_group(
    label: str,
    options: list[str],
    key_prefix: str,
    *,
    seed: list[str] | None = None,
    columns: int = 2,
    help_text: str = "",
) -> list[str]:
    """多选控件：用 checkbox 网格实现，**彻底不依赖 placeholder**。

    为什么不再用 st.multiselect：
    它未选中时会显示内置英文占位符 "Choose options"，而汉化用的
    placeholder 参数是 1.26+ 才有的。低版本一旦剥掉该参数，英文就会
    露出来。checkbox 在所有版本语法完全一致、天然没有占位符，选项
    文字本身就是中文，因此任何版本都不可能显示英文。

    :param seed: 需要预先勾选的选项（用于「调整条件」回填）
    :param columns: 一行放几个，侧边栏建议 2 列
    """
    st.markdown(f"<div class='field-label'>{escape(label)}</div>", unsafe_allow_html=True)
    if help_text:
        st.caption(help_text)

    seed_set = set(seed or [])
    chosen: list[str] = []
    row: list[Any] = []
    for index, option in enumerate(options):
        slot = index % columns
        if slot == 0:
            row = st.columns(columns)
        key = f"{key_prefix}_{index}"
        if key not in st.session_state:
            st.session_state[key] = option in seed_set
        if row[slot].checkbox(option, key=key):
            chosen.append(option)
    return chosen


def _current_group(prefix: str, name: str, options: list[str]) -> list[str]:
    """从 session_state 读出当前勾选项，供「调整条件」面板回填。"""
    out: list[str] = []
    for index, option in enumerate(options):
        if st.session_state.get(f"{prefix}{name}_{index}"):
            out.append(option)
    return out


def _sync_select(key: str, options: list[str], prefer: str | None = None) -> None:
    """下拉框取值兜底：当前值不在候选里就重置，避免 Streamlit 因非法默认值报错。"""
    if not options:
        return
    if key not in st.session_state or st.session_state[key] not in options:
        st.session_state[key] = prefer if prefer in options else options[0]


def _companion_picker(prefix: str, seed: list[str] | None = None) -> list[str]:
    """同行人：checkbox 网格多选，10 类人群（含团建 / 孕妇 / 无障碍）。

    刻意不使用 st.pills（需较新版本，低版本 AttributeError），
    也不使用 st.multiselect（未选中时显示内置英文 "Choose options"）。
    """
    return checkbox_group(
        "同行人",
        COMPANION_OPTIONS,
        key_prefix=f"{prefix}companions",
        seed=seed or [],
        columns=2,
        help_text="可多选，方便我安排节奏与体力消耗",
    )


def plain_companions(values: list[str]) -> list[str]:
    """Emoji 选项 -> 纯文本标签（按索引精确对应，送给 Agent 的仍是干净的词）。"""
    out: list[str] = []
    for value in values or []:
        text = str(value)
        idx = COMPANION_OPTIONS.index(text) if text in COMPANION_OPTIONS else -1
        label = COMPANION_LABELS[idx] if idx >= 0 else None
        if label is None:
            for plain in COMPANION_LABELS:
                if plain in text:
                    label = plain
                    break
        if label and label not in out:
            out.append(label)
    return out


CONSTRAINT_DEFAULTS: dict[str, Any] = {
    "country": "🇨🇳 中国",
    "province": "广东省",
    "city": "梅州市",
    "days": 2,
    "people": 2,
    "budget": 3000,
    "prefs": [],        # 带 Emoji 的偏好选项列表
    "pref_note": "",    # 其他补充需求（自由填写）
    "companions": [],
    "request": "想去有当地文化特色的地方，节奏轻松些",
}


def _date_bounds() -> tuple[date, date]:
    """出发日期可选范围：今天前 5 年 ~ 今天后 5 年（2 月 29 日兜底）。"""
    today = date.today()

    def shift(years: int) -> date:
        try:
            return today.replace(year=today.year + years)
        except ValueError:  # 2 月 29 日落到非闰年
            return today.replace(year=today.year + years, month=2, day=28)

    return shift(-5), shift(5)


def _span_days(start_day: date, end_day: date) -> int:
    """行程天数 = 回程 - 出发 + 1（含首尾），同日往返记 1 天。"""
    try:
        span = (end_day - start_day).days + 1
    except (TypeError, AttributeError):
        return CONSTRAINT_DEFAULTS["days"]
    return max(1, span)


def _constraint_fields(prefix: str, *, seed: dict[str, Any] | None = None) -> dict[str, Any]:
    """渲染三组约束控件，返回 payload。

    payload 除 Agent 需要的 8 个字段外，另附 country / province /
    preferences_list / preferences_note 等纯 UI 字段用于回填；
    run_agent() 会裁掉它们，保证送给 Agent 的仍是原来那 8 个。
    """
    base: dict[str, Any] = dict(CONSTRAINT_DEFAULTS)
    if seed:
        base.update({k: v for k, v in seed.items() if v is not None})

    def k(name: str) -> str:
        return f"{prefix}{name}"

    # ---------------- 📍 基础信息：国家 -> 省份 -> 城市 ----------------
    st.markdown("<div class='sec-title'>📍 基础信息</div>", unsafe_allow_html=True)

    country_options = list(DESTINATION_TREE)
    _sync_select(k("country"), country_options, base.get("country"))
    country = st.selectbox("目的地（国家 / 地区）", country_options, key=k("country"))

    province_options = list(DESTINATION_TREE.get(country, {}))
    _sync_select(k("province"), province_options, base.get("province"))
    province = st.selectbox("省份 / 直辖市", province_options, key=k("province"))

    city_options = list(DESTINATION_TREE.get(country, {}).get(province, []))
    _sync_select(k("city"), city_options, base.get("city"))
    city = st.selectbox("城市", city_options, key=k("city"))

    min_date, max_date = _date_bounds()
    seed_state(k("start"), base.get("start") or (date.today() + timedelta(days=1)))
    start_date = st.date_input(
        "出发日期",
        min_value=min_date,
        max_value=max_date,
        key=k("start"),
        help="可选范围：今天往前 5 年到往后 5 年",
    )

    # ---------------- 回程日期：天数由两个日期自动推算 ----------------
    # 原先的「行程天数」滑块改为「回程日期」，天数 = 回程 - 出发 + 1。
    end_key = k("end")
    wanted_span = max(0, int(base.get("days") or CONSTRAINT_DEFAULTS["days"]) - 1)
    fallback_end = min(start_date + timedelta(days=wanted_span), max_date)
    if end_key not in st.session_state:
        st.session_state[end_key] = base.get("end") or fallback_end
    # 回程不得早于出发、也不得越出 5 年窗口；须在控件实例化前修正，否则越界报错
    if st.session_state[end_key] < start_date:
        st.session_state[end_key] = start_date
    if st.session_state[end_key] > max_date:
        st.session_state[end_key] = max_date
    end_date = st.date_input(
        "回程日期",
        min_value=start_date,
        max_value=max_date,
        key=end_key,
        help="与出发日期一起决定行程天数；可选范围同样为 5 年内",
    )
    days = _span_days(start_date, end_date)
    nights = days - 1
    st.caption(
        f"共 {days} 天{' ' + str(nights) + ' 晚' if nights else ''} · "
        f"{start_date:%m月%d日} 出发，{end_date:%m月%d日} 返程"
    )

    # ---------------- 👥 人员与预算：取消上限 ----------------
    st.markdown("<hr class='sec-line'>", unsafe_allow_html=True)
    st.markdown("<div class='sec-title'>👥 人员与预算</div>", unsafe_allow_html=True)

    seed_state(k("people"), base["people"])
    people = st.number_input(
        "出行人数",
        min_value=1,
        max_value=999999,
        step=1,
        key=k("people"),
        help="公司团建等大团体可直接填人数，上限 99 万",
    )

    seed_state(k("budget"), base["budget"])
    budget = st.number_input(
        "总预算（元）",
        min_value=200,
        max_value=9999999,
        step=100,
        key=k("budget"),
        help="人数较多的团体可以填更大的预算",
    )

    companions = _companion_picker(prefix, seed=list(base["companions"]))

    # ---------------- 🎯 偏好设定：多选 + 自由补充 ----------------
    st.markdown("<hr class='sec-line'>", unsafe_allow_html=True)
    st.markdown("<div class='sec-title'>🎯 偏好设定</div>", unsafe_allow_html=True)

    preferences = checkbox_group(
        "特殊偏好",
        PREFERENCE_OPTIONS,
        key_prefix=k("prefs"),
        seed=list(base["prefs"]),
        columns=2,
        help_text="可多选；没找到合适的，可在下方「其他补充需求」里自由填写",
    )

    seed_state(k("pref_note"), base["pref_note"])
    pref_note = st.text_input(
        "其他补充需求",
        key=k("pref_note"),
        placeholder="未在以上选项中找到的偏好，请填在这里...",
    )

    seed_state(k("request"), base["request"])
    request = st.text_area(
        "行程整体诉求",
        key=k("request"),
        height=88,
        placeholder="例如：想去有当地文化特色的地方，节奏轻松些",
    )

    labels = [
        PREFERENCE_LABELS[PREFERENCE_OPTIONS.index(p)]
        for p in preferences
        if p in PREFERENCE_OPTIONS
    ]
    pref_text = "、".join(labels)
    note = pref_note.strip()
    if note:
        pref_text = f"{pref_text}；{note}" if pref_text else note

    return {
        "country": country,
        "province": province,
        "city": city,
        "days": int(days),
        "people": int(people),
        "budget": float(budget),
        "start_date": start_date.isoformat(),
        "end": end_date.isoformat(),
        "companions": plain_companions(companions),
        "preferences": pref_text,
        "preferences_list": list(preferences),
        "preferences_note": note,
        "request": request,
    }


def render_sidebar() -> tuple[dict[str, Any], str, bool]:
    with st.sidebar:
        st.header("行程约束")
        hint = st.session_state.pop("_preset_hint", None)
        if hint:
            st.caption(f"已套用示例：{hint}")

        payload = _constraint_fields("f_")
        run_clicked = full_width_button("开始规划", kind="primary")
    return payload, payload["request"], run_clicked


# ---------------------------------------------------------------------------
# 规划过程提示（C 端口吻：说用户在意的，不说系统在跑什么）
# ---------------------------------------------------------------------------
# 送给 travel_agent 的字段白名单：与改造前的 8 个字段完全一致。
# UI 新增的 country / province / preferences_list / preferences_note 只用于回填控件，
# 不能进 Agent，否则会污染原有接口。
AGENT_PAYLOAD_KEYS = (
    "city",
    "days",
    "people",
    "budget",
    "start_date",
    "companions",
    "preferences",
    "request",
)


def agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """裁出 Agent 需要的字段，保证接口与改造前一致。"""
    return {key: payload[key] for key in AGENT_PAYLOAD_KEYS if key in payload}


def run_agent(request: str, payload: dict[str, Any]) -> dict[str, Any]:
    """执行规划：只给用户看「正在为您做什么」，不暴露任何技术细节。"""
    entries: list[dict[str, Any]] = []
    payload = agent_payload(payload)
    with status_box("💡 正在为您查阅地图、对比天气并规划最优路线，请稍候…", expanded=True) as status:
        stage = st.empty()
        steps: list[str] = []

        def add_step(text: str, pause: float = 0.5) -> None:
            steps.append(text)
            stage.markdown("\n\n".join(steps))
            time.sleep(pause)

        add_step("🗺️ 正在寻找当地值得一去的景点与特色美食…")
        add_step("🌤️ 正在对比这几天的天气，把户外安排放到好天气…")
        add_step("🚗 正在规避拥堵路线，让每天的路程别太赶…")

        try:
            engine = agent.TravelAgent(trace_callback=lambda entry: entries.append(entry))
            result = engine.run(request, payload)
        except Exception:
            status.update(label="规划没有完成", state="error", expanded=True)
            raise

        iterations = int(result.get("iterations") or 1)
        if iterations > 1:
            add_step("✨ 为了让行程更顺畅，正在微调时间安排…", pause=0.4)
        status.update(label="行程已经准备好啦！", state="complete", expanded=False)
    return result


# ---------------------------------------------------------------------------
# 冲突自动重排：营业时间撞车时自行顺延，不把报错丢给用户
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")


def _hhmm_to_min(text: str) -> int | None:
    """把 '13:20' / '13：20' 转成分钟数；解析不了就返回 None。"""
    m = _TIME_RE.search(str(text or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def _min_to_hhmm(value: int) -> str:
    value = max(0, min(value, 24 * 60 - 1))
    return f"{value // 60:02d}:{value % 60:02d}"


def _open_segments(open_time: str) -> list[tuple[int, int]]:
    """解析开放时间，支持 '09:00-12:00, 13:30-17:00' 这类多段写法。"""
    text = str(open_time or "")
    if not text:
        return []
    segments: list[tuple[int, int]] = []
    for chunk in re.split(r"[，,；;、/]|\s{2,}|和|及", text):
        times = _TIME_RE.findall(chunk)
        if len(times) >= 2:
            start = int(times[0][0]) * 60 + int(times[0][1])
            end = int(times[1][0]) * 60 + int(times[1][1])
            if end > start:
                segments.append((start, end))
    return segments


def _shift_item(item: dict[str, Any], delta: int) -> bool:
    """把某个行程节点整体顺延 delta 分钟，返回是否真的改动。"""
    if not delta:
        return False
    start = _hhmm_to_min(item.get("start"))
    if start is None:
        return False
    changed = False
    item["start"] = _min_to_hhmm(start + delta)
    changed = True
    end = _hhmm_to_min(item.get("end"))
    if end is not None:
        item["end"] = _min_to_hhmm(end + delta)
    return changed


def reschedule_conflicts(days: list[dict[str, Any]]) -> list[str]:
    """营业时间冲突时自动顺延游览时间，并返回柔和的说明文案。

    规则：若开始时间落在闭馆区间内，则顺延到该馆当天最近的可入馆时刻；
    时长保持不变，绝不把「校验失败」抛给用户。
    """
    notes: list[str] = []
    for day in days:
        for item in day.get("items") or []:
            segments = _open_segments(item.get("open_time") or "")
            if not segments:
                continue
            start = _hhmm_to_min(item.get("start"))
            if start is None:
                continue
            inside = any(begin <= start < finish for begin, finish in segments)
            if inside:
                continue
            later = sorted(b for b, _ in segments if b > start)
            if not later:
                continue
            target = later[0]
            # 只在「撞上午休/闭馆」这种明显不合理的情况下调整
            nearest_end = max(f for _, f in segments if f <= target) if any(
                f <= target for _, f in segments
            ) else None
            if nearest_end is None or not (nearest_end - 30 <= start < target):
                continue
            delta = target - start
            if _shift_item(item, delta):
                name = str(item.get("name") or "这处景点")
                notes.append(
                    f"为了让您的行程更顺畅，已将{name}的参观时间顺延至 "
                    f"{_min_to_hhmm(target)} 开馆后再入场~"
                )
    return notes


# ---------------------------------------------------------------------------
# 任务 4：卡片式时间轴行程
# ---------------------------------------------------------------------------
def _weather_icon(note: str) -> str:
    text = note or ""
    if "雷" in text:
        return "⛈️"
    if "雪" in text:
        return "❄️"
    if "雨" in text:
        return "🌧️"
    if "阴" in text or "云" in text or "多云" in text:
        return "☁️"
    if "晴" in text:
        return "☀️"
    return "🌤️"


def _day_theme(items: list[dict[str, Any]]) -> str:
    names = [str(i.get("name")) for i in items if str(i.get("type") or "") in {"attraction", "food"} and i.get("name")]
    if not names:
        return "自由安排"
    return " · ".join(names[:2])


def _nav_anchor(item: dict[str, Any]) -> str:
    lng, lat = item.get("lng"), item.get("lat")
    if lng is None or lat is None:
        return ""
    name = quote(str(item.get("name") or ""))
    href = f"https://uri.amap.com/marker?position={lng},{lat}&name={name}"
    return f"<a class='tl-nav' target='_blank' rel='noopener' href='{href}'>📍 导航</a>"


def _weather_temp(note: str) -> str:
    """从天气描述里抠出温度，如「15-20℃」「18°C」；抠不到就留空，绝不编造。"""
    text = note or ""
    span = re.search(r"(-?\d+)\s*[~～\-—至]\s*(-?\d+)", text)
    if span:
        return f"{span.group(1)}~{span.group(2)}℃"
    single = re.search(r"(-?\d+)\s*(?:℃|°C)", text)
    if single:
        return f"{single.group(1)}℃"
    return ""


def _transport_line(item: dict[str, Any]) -> str:
    """把交通方式写成一句人话，例如「🚶 步行 0.8 公里 · 0 元」。"""
    mode = str(item.get("transport") or "")
    icon = {"walking": "🚶", "driving": "🚗", "transit": "🚌"}.get(mode, "🚗")
    label = TRANSPORT_LABEL.get(mode, "交通")
    bits = [f"{icon} {label}"]
    try:
        walk_km = float(item.get("walk_km") or 0)
    except (TypeError, ValueError):
        walk_km = 0.0
    if walk_km:
        bits.append(f"{walk_km:g} 公里")
    try:
        cost = float(item.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    bits.append(f"{cost:g} 元" if cost else "免费")
    return " · ".join(bits)


def _transport_only(item: dict[str, Any]) -> str:
    """只描述交通方式与距离，不含费用（费用由独立的「花费」列呈现）。"""
    mode = str(item.get("transport") or "")
    icon = {"walking": "🚶", "driving": "🚗", "transit": "🚌"}.get(mode, "🚗")
    label = TRANSPORT_LABEL.get(mode, "交通")
    bits = [f"{icon} {label}"]
    try:
        walk_km = float(item.get("walk_km") or 0)
    except (TypeError, ValueError):
        walk_km = 0.0
    if walk_km:
        bits.append(f"{walk_km:g} 公里")
    return " · ".join(bits)


def _node_html(item: dict[str, Any]) -> str:
    """单个行程节点：时间 -> 地点 -> 交通 -> 提示 -> 导航。"""
    kind = str(item.get("type") or "attraction")
    icon = TYPE_ICON.get(kind, "📍")
    label = TYPE_LABEL.get(kind, "")
    name = escape(str(item.get("name") or "未命名地点"))
    start = escape(str(item.get("start") or ""))
    end = escape(str(item.get("end") or ""))
    time_txt = f"{start} – {end}" if start and end else (start or end or "时间待定")
    title = f"{icon} {label}：{name}" if label else f"{icon} {name}"

    note = escape(str(item.get("note") or ""))
    note_html = f"<div class='nd-note'>💡 {note}</div>" if note else ""
    open_time = str(item.get("open_time") or "")
    open_html = f"<div class='nd-line'>🕘 开放时间 {escape(open_time)}</div>" if open_time else ""

    return (
        "<div class='nd'>"
        "<div class='nd-dot'></div>"
        "<div class='nd-main'>"
        f"<div class='nd-time'>🕒 {time_txt}</div>"
        f"<div class='nd-name'>{title}</div>"
        f"<div class='nd-line'>{_transport_line(item)}</div>"
        f"{open_html}{note_html}"
        f"<div class='nd-foot'>{_nav_anchor(item)}</div>"
        "</div></div>"
    )


def _timeline_html(items: list[dict[str, Any]]) -> str:
    return "<div>" + "".join(_node_html(item) for item in items) + "</div>"


def render_day_card(day: dict[str, Any]) -> None:
    items = day.get("items") or []
    note = str(day.get("weather_note") or "")
    icon = _weather_icon(note)
    temp = _weather_temp(note)
    weather_badge = f"{icon} {temp}".strip() or icon
    day_no = int(day.get("day") or 1)
    theme = _day_theme(items)

    with card():
        st.markdown(
            "<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap;'>"
            f"<div><div class='day-title'>第 {day_no} 天</div>"
            f"<div class='day-theme2'>{escape(theme)}</div></div>"
            f"<div class='day-weather'>{weather_badge}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        if not items:
            st.caption("本日暂无安排，可以自由逛逛。")
            return
        st.markdown(_timeline_html(items), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 结果区（C 端化：看板 / AI 伴游 / 游记时间轴 / 温暖提示）
# ---------------------------------------------------------------------------
def _first_weather(days: list[dict[str, Any]]) -> tuple[str, str]:
    """取第一天的天气与温度做看板；没数据就留空，不编造。"""
    for day in days:
        note = str(day.get("weather_note") or "")
        if note:
            return _weather_icon(note), _weather_temp(note)
    return "🌤️", ""


def _weather_desc(note: str) -> str:
    """去掉温度后的天气描述，避免与看板上的温度重复。"""
    text = (note or "").strip()
    text = re.sub(r"(-?\d+)\s*[~～\-—至]\s*(-?\d+)\s*(?:℃|°C)", "", text)
    text = re.sub(r"(-?\d+)\s*(?:℃|°C)", "", text)
    text = re.sub(r"[，,、\s]{2,}", " ", text).strip(" ，,、")
    return text


def render_dashboard(result: dict[str, Any], itinerary: dict[str, Any], days: list[dict[str, Any]]) -> None:
    """顶部看板：目的地、天气、预估花费（不展示耗时等工程指标）。"""
    c = result["constraints"]
    budget = result["budget"]
    total = float(budget.get("total") or 0)
    cap = float(c.get("budget") or 0)
    city = itinerary.get("city") or c.get("city") or ""
    weather_icon, temp = _first_weather(days)

    weather_value = f"{weather_icon} {temp}".strip() if temp else f"{weather_icon} 天气已就绪"
    first_note = str(days[0].get("weather_note") or "") if days else ""
    weather_sub = _weather_desc(first_note) or "以当地实际为准"

    st.markdown(
        "<div class='dash'>"
        f"<div class='dash-card'><div class='dash-label'>目的地</div>"
        f"<div class='dash-value'>📍 {escape(str(city))}</div>"
        f"<div class='dash-sub'>{int(c.get('days') or 0)} 天 · {int(c.get('people') or 0)} 人同行</div></div>"
        f"<div class='dash-card'><div class='dash-label'>天气</div>"
        f"<div class='dash-value'>{weather_value}</div>"
        f"<div class='dash-sub'>{escape(weather_sub)}</div></div>"
        "<div class='dash-card dash-amber'><div class='dash-label'>预估花费</div>"
        f"<div class='dash-value'>¥{total:,.0f}</div>"
        f"<div class='dash-sub'>预算 ¥{cap:,.0f}</div></div>"
        "</div>",
        unsafe_allow_html=True,
    )


def render_budget(budget: dict[str, Any], cap: float) -> None:
    """预算进度条 + 明细，替代原来的预算表格。"""
    total = float(budget.get("total") or 0)
    ratio = (total / cap) if cap > 0 else 0.0
    percent = max(0.0, min(ratio, 1.0)) * 100
    fill_cls = "over" if ratio > 1 else ("warn" if ratio > 0.85 else "")
    left = max(0.0, cap - total)

    if ratio > 1:
        hint = f"已超出预算 ¥{total - cap:,.0f}，建议调整住宿或餐饮标准"
    else:
        hint = f"还剩 ¥{left:,.0f} 可以自由支配"

    st.markdown(
        "<div class='dash-card'>"
        "<div class='dash-label'>💰 预算使用情况</div>"
        f"<div class='dash-value'>¥{total:,.0f} <span style='font-size:14px;color:#94A3B8;font-weight:500'>/ ¥{cap:,.0f}（占 {percent:.0f}%）</span></div>"
        f"<div class='bar-wrap'><div class='bar-fill {fill_cls}' style='width:{percent:.1f}%'></div></div>"
        f"<div class='dash-sub'>{escape(hint)}</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    breakdown = budget.get("breakdown") or {}
    if breakdown:
        rows = []
        for key, value in breakdown.items():
            try:
                amount = float(value)
            except (TypeError, ValueError):
                continue
            rows.append(
                f"<div class='bill-row'><span>{escape(zh_labels.budget_label(key) if hasattr(zh_labels, 'budget_label') else str(key))}</span>"
                f"<span>¥{amount:,.0f}</span></div>"
            )
        if rows:
            st.markdown("<div class='dash-card'>" + "".join(rows) + "</div>", unsafe_allow_html=True)


def render_ai_bubble(itinerary: dict[str, Any], c: dict[str, Any]) -> None:
    """AI 伴游口吻的开场说明，代替冷冰冰的 st.info。"""
    summary = str(itinerary.get("summary") or "").strip()
    companions = c.get("companions") or []
    prefs = str(c.get("preferences") or "").strip()
    city = str(itinerary.get("city") or c.get("city") or "")

    reasons: list[str] = []
    if companions:
        reasons.append("同行有" + "、".join(str(x) for x in companions))
    if prefs:
        reasons.append(f"也记下了您偏好「{prefs}」")
    tail = "，所以节奏安排得比较宽松" if reasons else "，节奏与花费都帮您算好了"

    if summary:
        text = f"为您规划了这条 {city} 线路。{summary}"
    else:
        text = f"为您规划了这条 {city} 线路"

    st.markdown(
        "<div class='ai-bubble'>"
        "<div class='ai-avatar'>🧭</div>"
        "<div>"
        "<div class='ai-name'>AI 旅行规划师</div>"
        f"<div class='ai-text'>{escape(text)}{escape(tail)}。往下看看每天的安排，点「📍 导航」可直接唤起高德地图。</div>"
        "</div></div>",
        unsafe_allow_html=True,
    )


def render_tips(tips: list[str]) -> None:
    """注意事项 -> 带 Emoji 的柔和圆角卡片，语气像私人导游。"""
    if not tips:
        return
    st.subheader("出行小贴士")
    for tip in tips:
        st.markdown(
            f"<div class='tip-card'>💡 {escape(_soften_tip(str(tip)))}</div>",
            unsafe_allow_html=True,
        )


_TIP_REWRITES: tuple[tuple[str, str], ...] = (
    ("务必按开放时间入馆", "建议留意两馆的午休时间哦（12:00-13:30），避开这个时段能玩得更尽兴~"),
    ("务必按开放时间前往", "建议避开中午的休息时段，去之前看一眼开门时间会更从容哦~"),
    ("务必准时", "建议提前一点点出发，路上会更从容哦~"),
    ("带薄外套", "记得带件薄外套"),
    ("备伞", "带把伞"),
    ("注意防晒", "防晒别忘了，帽子或防晒霜都行哦"),
    ("请提前预约", "记得提前预约一下，免得白跑一趟"),
)


def _soften_tip(text: str) -> str:
    """把生硬的提示语改成贴心导游口吻；不认识的就补一句软尾。"""
    result = str(text or "").strip()
    # 去掉模型自带的前置符号/Emoji（如 ⚠️、❗、-），避免与卡片图标重复
    while result and not (result[0].isalnum() or "\u4e00" <= result[0] <= "\u9fff"):
        result = result[1:]
    result = result.strip()
    for hard, soft in _TIP_REWRITES:
        if hard in result:
            result = result.replace(hard, soft)
    if "！" in result:
        result = result.replace("！", "~")
    if result and not result.endswith(("~", "哦", "呢", "。", "啦", "吧")):
        result = result.rstrip("。") + "~"
    return result


@contextmanager
def popover_or_expander(label: str):
    """弹出层：优先 st.popover，老版本退回 st.expander。"""
    factory = getattr(st, "popover", None)
    if callable(factory):
        try:
            with factory(label) as box:
                yield box
            return
        except TypeError:
            pass
    with st.expander(label, expanded=False) as box:
        yield box


def render_adjust_panel() -> None:
    """结果页的「调整条件」：不切回侧边栏即可改参数并重新规划。

    用 "q_" 前缀渲染，避开侧边栏 "f_" 的控件 key；
    提交时只暂存，由下次运行顶部统一写回。
    """
    current = {
        "country": st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"]),
        "province": st.session_state.get("f_province", CONSTRAINT_DEFAULTS["province"]),
        "city": st.session_state.get("f_city", CONSTRAINT_DEFAULTS["city"]),
        "days": st.session_state.get("f_days", CONSTRAINT_DEFAULTS["days"]),
        "people": st.session_state.get("f_people", CONSTRAINT_DEFAULTS["people"]),
        "budget": st.session_state.get("f_budget", CONSTRAINT_DEFAULTS["budget"]),
        "prefs": _current_group("f_", "prefs", PREFERENCE_OPTIONS)
        or list(CONSTRAINT_DEFAULTS["prefs"]),
        "pref_note": st.session_state.get("f_pref_note", CONSTRAINT_DEFAULTS["pref_note"]),
        "companions": _current_group("f_", "companions", COMPANION_OPTIONS),
        "start": st.session_state.get("f_start"),
        "end": st.session_state.get("f_end"),
        "request": st.session_state.get("f_request", CONSTRAINT_DEFAULTS["request"]),
    }
    with popover_or_expander("✏️ 调整条件并重新规划"):
        new_payload = _constraint_fields("q_", seed=current)
        if full_width_button("用新条件重新规划", kind="primary", key="q_rerun"):
            # 只暂存，不直接改 f_*：这些控件本轮已实例化，直接改会抛异常。
            st.session_state["_pending_constraints"] = new_payload
            st.session_state["_rerun_request"] = new_payload["request"]
            st.rerun()


def _table_rows(days: list[dict[str, Any]]) -> str:
    """把行程压成 5 列：时间 / 安排 / 交通方式 / 花费 / 贴心提示。"""
    parts: list[str] = []
    for day in days:
        items = day.get("items") or []
        day_no = int(day.get("day") or 1)
        theme = _day_theme(items)
        head = f"第 {day_no} 天" + (f" · {theme}" if theme else "")
        parts.append(
            "<tr class='itn-day-row'><td colspan='5'>"
            f"🗓️ {escape(head)}"
            "</td></tr>"
        )
        if not items:
            parts.append(
                "<tr><td class='itn-time'>—</td>"
                "<td colspan='4' style='color:#94A3B8'>这天没有固定安排，可以自由逛逛。</td></tr>"
            )
            continue
        for item in items:
            kind = str(item.get("type") or "attraction")
            icon = TYPE_ICON.get(kind, "📍")
            label = TYPE_LABEL.get(kind, "")
            name = escape(str(item.get("name") or "未命名地点"))
            title = f"{icon} {name}" + (f"<span style='color:#94A3B8;font-size:12px'>（{label}）</span>" if label else "")
            start = str(item.get("start") or "")
            end = str(item.get("end") or "")
            time_txt = f"{start}–{end}" if start and end else (start or end or "时间待定")
            try:
                cost = float(item.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            cost_txt = f"¥{cost:g}" if cost else "免费"
            note = str(item.get("note") or "").strip()
            parts.append(
                "<tr>"
                f"<td class='itn-time'>{escape(time_txt)}</td>"
                f"<td>{title}</td>"
                f"<td>{_transport_only(item)}</td>"
                f"<td class='itn-cost'>{cost_txt}</td>"
                f"<td class='itn-tip'>{escape(note) if note else '—'}</td>"
                "</tr>"
            )
    return "".join(parts)


def render_itinerary_table(days: list[dict[str, Any]]) -> None:
    """暗色护眼的行程速览表：无生硬边框，像一本手账。"""
    if not days:
        return
    rows = _table_rows(days)
    if not rows:
        return
    st.markdown(
        "<div class='itn-wrap'><table class='itn-table'>"
        "<thead><tr>"
        "<th style='width:13%'>时间</th>"
        "<th style='width:32%'>安排</th>"
        "<th style='width:24%'>交通方式</th>"
        "<th style='width:11%'>花费</th>"
        "<th style='width:20%'>贴心提示</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def show_result(result: dict[str, Any]) -> None:
    c = result["constraints"]
    budget = result["budget"]
    itinerary = result["itinerary"]
    days = itinerary.get("days") or []

    # 冲突自动重排：营业时间撞车时自行顺延，不向用户报错
    soften_notes = reschedule_conflicts(days)

    render_dashboard(result, itinerary, days)
    render_adjust_panel()
    render_ai_bubble(itinerary, c)

    for note in soften_notes:
        st.markdown(f"<div class='soft-note'>✨ {escape(note)}</div>", unsafe_allow_html=True)

    # 标题行右侧放下载按钮，替代底部折叠面板
    head_cols = st.columns([3, 1])
    with head_cols[0]:
        st.subheader("行程一览")
    with head_cols[1]:
        full_width_download(
            "📥 下载行程单",
            data=result["markdown"],
            file_name=f"{c['city']}-{c['days']}天行程单.md",
            mime="text/markdown",
        )

    # 兜底：模型没给出 days 结构时，用原来的表格数据渲染，避免开天窗
    if not days:
        rows = zh_labels.plan_rows(itinerary) or []
        if rows:
            st.warning("行程结构不完整，以下为可用信息的整理结果。")
            for row in rows:
                st.write(row)
        else:
            st.info("这次没有生成可展示的行程，请调整需求后重试。")

    render_itinerary_table(days)

    if days:
        st.subheader("每日详情")
        for day in days:
            render_day_card(day)

    st.subheader("预算与花费")
    render_budget(budget, float(c.get("budget") or 0))

    render_tips(itinerary.get("tips") or [])


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    setup_detail_log()
    apply_pending_constraints()
    apply_pending_preset()
    inject_css()

    # 全局 Header 已下线：品牌名收进空状态页，避免顶部黑边与强制标题
    missing = config.require_keys()
    if missing:
        st.warning(
            "缺少密钥：" + "、".join(zh_labels.key_label(name) for name in missing) + "。"
            "本地运行请把 .env.example 复制为 .env 并填写对应变量后重启；"
            "线上部署请打开右上角「设置 → 密钥」，按示例格式粘贴后点保存。"
        )

    payload, request, run_clicked = render_sidebar()

    # 结果页「调整条件」提交后，用同一套 payload 流程重算
    rerun_request = st.session_state.pop("_rerun_request", None)
    if rerun_request is not None:
        run_clicked = True
        request = str(rerun_request)

    if run_clicked:
        st.session_state.pop("result", None)
        try:
            st.session_state["result"] = run_agent(request, payload)
        except agent.AgentError as exc:
            st.error(f"这次没能规划成功：{zh_labels.friendly_error(exc)}")
        except Exception:  # noqa: BLE001 - 界面层兜底，避免白屏
            st.error("出了点小状况，请稍后再试一次。")

    result = st.session_state.get("result")
    if result:
        show_result(result)
    elif not run_clicked:
        render_empty_state()


if __name__ == "__main__":
    main()
