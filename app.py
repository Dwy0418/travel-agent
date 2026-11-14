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
        "tag": "🔥 爆款",
        "img": "https://images.unsplash.com/photo-1760898684796-02e46d5053c4?q=80&w=900&auto=format&fit=crop",
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
        "tag": "🎨 室内避雨",
        "img": "https://images.unsplash.com/photo-1735605917461-4c1b77a6616f?q=80&w=900&auto=format&fit=crop",
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
        "tag": "🏔️ 风光大片",
        "img": "https://images.unsplash.com/photo-1663145765508-a79f2d968ccc?q=80&w=900&auto=format&fit=crop",
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
# 目的地级联推荐数据：全国池 / 省级 / 市级，全部由 CITY_CARDS 派生
# ---------------------------------------------------------------------------
def _img(photo_id: str) -> str:
    """拼接 Unsplash 固定图源：统一 900 宽、等比裁切，保证卡片清晰度。"""
    return f"https://images.unsplash.com/{photo_id}?q=80&w=900&auto=format&fit=crop"


# 卡片配图：每座城市 6 张，顺序与下面 RAW / EXTRA_RAW 的卡片一一对应。
# 选图原则：画面必须与该行程的文案同义（熊猫配熊猫、水利工程配堰坝），
# 不再复用「看起来像风景」的随机图，避免出现成都配罗马斗兽场这类错配。
IMG = {
    # 梅州：围龙屋 / 客家古建 / 茶田 / 古村 / 客家菜 / 温泉
    "mz1": _img("photo-1760898684796-02e46d5053c4"),
    "mz2": _img("photo-1745989498560-a1f1cd9fa17a"),
    "mz3": _img("photo-1743401462369-44e568d93027"),
    "mz4": _img("photo-1784534922907-ba17e790010a"),
    "mz5": _img("photo-1789999376136-f55049c3a26d"),
    "mz6": _img("photo-1783094724116-ff5f8362a581"),
    # 广州：早茶 / 广州塔 / 长隆 / 沙面 / 珠江夜游 / 白云山
    "gz1": _img("photo-1763994679303-fa1f6a7bf687"),
    "gz2": _img("photo-1563090162-6b4c2a20d658"),
    "gz3": _img("photo-1534567110243-8875d64ca8ff"),
    "gz4": _img("photo-1665581378780-6950687b0806"),
    "gz5": _img("photo-1649147857403-ea32b99fc0b8"),
    "gz6": _img("photo-1663145766163-8cca1253b6c9"),
    # 深圳：深圳湾 / 创意园 / 科技馆 / 海洋馆 / 大鹏海岸 / 美食
    "sz1": _img("photo-1661178581714-a344b67c8691"),
    "sz2": _img("photo-1569783721854-33a99b4c0bae"),
    "sz3": _img("photo-1764253340517-9adc9a839970"),
    "sz4": _img("photo-1769234428281-c108a6053cd3"),
    "sz5": _img("photo-1660723257840-9155e2de0745"),
    "sz6": _img("photo-1707013533606-62919aa3aa29"),
    # 成都：熊猫 / 老城 / 都江堰 / 熊猫义工 / 川菜 / 青城后山
    "cd1": _img("photo-1540126034813-121bf29033d2"),
    "cd2": _img("photo-1713844114227-da3cba9ca5d1"),
    "cd3": _img("photo-1792756459557-363f49a35e73"),
    "cd4": _img("photo-1566487097168-e91a4f38bee2"),
    "cd5": _img("photo-1791409700484-e755b72c2b00"),
    "cd6": _img("photo-1792756459951-c6b19c5828c5"),
    # 甘孜：公路 / 新都桥 / 亚丁 / 日出 / 露营 / 草原
    "gzz1": _img("photo-1663145765508-a79f2d968ccc"),
    "gzz2": _img("photo-1652240166694-fa264d1519af"),
    "gzz3": _img("photo-1604688510517-5fb678ad4ab3"),
    "gzz4": _img("photo-1514241516423-6c0a5e031aa2"),
    "gzz5": _img("photo-1517824806704-9040b037703b"),
    "gzz6": _img("photo-1790157965324-c2e0b9df5286"),
    # 北京：故宫 / 长城 / 胡同 / 烤鸭 / 博物馆 / 京郊长城
    "bj1": _img("photo-1547981609-4b6bfe67ca0b"),
    "bj2": _img("photo-1508804185872-d7badad00f7d"),
    "bj3": _img("photo-1772764058009-e6cb2203d773"),
    "bj4": _img("photo-1765743691388-6a5608004b9c"),
    "bj5": _img("photo-1787244719525-358519ca2439"),
    "bj6": _img("photo-1608037521277-154cd1b89191"),
    # 上海：看展 / 外滩 / 咖啡 / 科技馆 / 舞台 / 本帮菜
    "sh1": _img("photo-1735605917461-4c1b77a6616f"),
    "sh2": _img("photo-1535356795203-50b2eb73f96c"),
    "sh3": _img("photo-1511081692775-05d0f180a065"),
    "sh4": _img("photo-1764253340521-c920fd89a66e"),
    "sh5": _img("photo-1503095396549-807759245b35"),
    "sh6": _img("photo-1789999375823-87fa75e49ff1"),
    # 杭州：西湖 / 龙井 / 西溪 / 灵隐 / 环湖 / 运河
    "hz1": _img("photo-1587153662967-c9d9977bf8d3"),
    "hz2": _img("photo-1743402810466-29a840f2c053"),
    "hz3": _img("photo-1786503316136-6235c13c7b49"),
    "hz4": _img("photo-1701070658926-5a7844890fb6"),
    "hz5": _img("photo-1669532055370-b0c282cb115f"),
    "hz6": _img("photo-1782662235606-2adc36529512"),
    # 大理：洱海 / 苍山 / 古城 / 喜洲 / 双廊 / 茶马古道
    "dl1": _img("photo-1690529070388-e259c3c31900"),
    "dl2": _img("photo-1715468932667-84581433613e"),
    "dl3": _img("photo-1794128774564-bd2b764e2218"),
    "dl4": _img("photo-1791446602731-45b8019bc147"),
    "dl5": _img("photo-1615874334296-35adc00bf71e"),
    "dl6": _img("photo-1787707260408-8d863a115c73"),
    # 西安：兵马俑 / 古都 / 大唐不夜城 / 回民街 / 陕历博 / 华山
    "xa1": _img("photo-1510108047773-263cc794fca3"),
    "xa2": _img("photo-1614555383820-941c466f1b52"),
    "xa3": _img("photo-1720451112427-1b93478b7417"),
    "xa4": _img("photo-1766801884391-bb142e51ca1f"),
    "xa5": _img("photo-1547253858-a72b17d5d455"),
    "xa6": _img("photo-1603127647343-16c5d38642e6"),
    # 厦门：鼓浪屿 / 海边 / 小吃 / 环岛路 / 集美 / 沙坡尾
    "xm1": _img("photo-1709229919646-3699ead369b5"),
    "xm2": _img("photo-1596188126441-41279e8f5e46"),
    "xm3": _img("photo-1604632910793-c0601f361b34"),
    "xm4": _img("photo-1648530021055-9d634c546d51"),
    "xm5": _img("photo-1786784962183-80d2cec02bf8"),
    "xm6": _img("photo-1697916193589-8f5706bf3c30"),
}

# (tag, img, title, desc, days, people, budget, companions, preferences, request)
RAW = {
    "梅州市": [
        ("🔥 爆款", 'mz1', "梅州客都悠闲 2 日游", "带老人慢节奏，围龙屋与客家博物馆", 2, 2, 3000,
         ["带老人"], ["少走路/休闲游", "室内为主"], "想去有当地文化特色的地方，节奏轻松些"),
        ("🍵 客家文化", 'mz2', "围龙屋 + 客家博物馆 2 日", "古建筑群与迁徙史，一圈看懂客家", 2, 2, 2600,
         ["情侣/夫妻"], ["文化历史", "出片率高"], "重点看围龙屋和客家博物馆"),
        ("🌿 茶田慢游", 'mz3', "雁南飞茶田康养 3 日", "茶山云雾、温泉，老人小孩都轻松", 3, 4, 4200,
         ["亲子家庭"], ["少走路/休闲游", "户外自然"], "想去茶田和温泉，节奏慢一点"),
    ],
    "广州市": [
        ("🥟 早茶必吃", 'gz1', "广州老城早茶 2 日", "陶陶居、上下九，一天五顿不重样", 2, 2, 2800,
         ["朋友结伴"], ["美食为主"], "主要想喝早茶和吃地道小吃"),
        ("🌉 珠江夜景", 'gz2', "广州塔 + 珠江夜游 2 日", "白天逛沙面，晚上看小蛮腰灯光", 2, 2, 3200,
         ["情侣/夫妻"], ["出片率高", "夜景为主"], "想看广州塔和珠江夜景"),
        ("👶 亲子长隆", 'gz3', "长隆亲子 3 日", "野生动物世界 + 欢乐世界，孩子玩到不肯走", 3, 3, 6000,
         ["带小孩", "亲子家庭"], ["亲子友好", "室内为主"], "以长隆为主，孩子 6 岁"),
    ],
    "深圳市": [
        ("🏙️ 都市海岸", 'sz1', "深圳湾 + 大梅沙 2 日", "骑行、看海、看对岸灯火", 2, 2, 3400,
         ["情侣/夫妻"], ["出片率高", "户外自然"], "想看海和城市天际线"),
        ("🎨 艺术街区", 'sz2', "华侨城创意园看展 2 日", "旧厂房改造的艺术区，遍地咖啡馆", 2, 2, 3000,
         ["独自出行"], ["室内为主", "出片率高"], "想看展和逛独立书店"),
        ("🚀 科技研学", 'sz3', "深圳科技亲子研学 2 日", "科技馆 + 人才公园，边玩边学", 2, 3, 3600,
         ["带小孩"], ["亲子友好", "室内为主"], "希望有科技馆这类研学内容"),
    ],
    "成都市": [
        ("🐼 熊猫 + 火锅", 'cd1', "成都熊猫基地 2 日", "早看熊猫下午喝茶，晚上火锅", 2, 2, 3000,
         ["情侣/夫妻"], ["美食为主", "出片率高"], "一定要看熊猫，也想吃火锅"),
        ("🏮 市井慢生活", 'cd2', "成都老城慢游 3 日", "宽窄巷子、人民公园、锦里", 3, 3, 4000,
         ["带老人"], ["少走路/休闲游", "文化历史"], "节奏慢，多喝茶少赶路"),
        ("🎋 都江堰青城山", 'cd3', "都江堰 + 青城山 2 日", "上午看水利工程，下午上山避暑", 2, 2, 2600,
         ["朋友结伴"], ["户外自然", "文化历史"], "想看水利工程和爬山"),
    ],
    "甘孜州": [
        ("🏔️ 风光大片", 'gzz1', "川西自驾特种兵 3 日", "高原风光与观景点，密度拉满", 3, 2, 8000,
         ["朋友结伴"], ["出片率高", "户外自然", "特种兵拉练"], "以自驾为主，多安排高原风光与观景点"),
        ("📷 摄影环线", 'gzz2', "新都桥摄影环线 4 日", "光影最好的川西，随手都是壁纸", 4, 2, 9000,
         ["朋友结伴"], ["出片率高", "户外自然"], "以摄影为主，愿意早起守日出"),
        ("⛰️ 高原轻徒步", 'gzz3', "稻城亚丁 4 日", "雪山、牛奶海，体力中等可完成", 4, 2, 8600,
         ["朋友结伴"], ["户外自然", "特种兵拉练"], "想看雪山和海子，能走 3 小时"),
    ],
    "北京市": [
        ("🏯 经典初访", 'bj1', "北京必打卡 3 日", "故宫、长城、颐和园一次走完", 3, 2, 5000,
         ["情侣/夫妻"], ["文化历史", "出片率高"], "第一次来北京，想走经典路线"),
        ("👴 老人友好", 'bj2', "北京慢游 4 日", "每天两景点，午后回酒店休息", 4, 3, 6500,
         ["带老人"], ["少走路/休闲游", "文化历史"], "带爸妈来，别太累"),
        ("🎨 展览与胡同", 'bj3', "北京看展 + 胡同 2 日", "798、国博，顺路逛南锣鼓巷", 2, 2, 3600,
         ["独自出行"], ["室内为主", "出片率高"], "想看展，也想去胡同里走走"),
        ("🍜 京味美食", 'bj4', "北京小吃深度 2 日", "豆汁、卤煮、烤鸭，两天吃个遍", 2, 2, 3200,
         ["朋友结伴"], ["美食为主"], "以吃为主，想吃地道小吃"),
        ("🎓 亲子研学", 'bj5', "北京亲子研学 3 日", "科技馆 + 天文馆 + 动物园", 3, 3, 5800,
         ["带小孩", "亲子家庭"], ["亲子友好", "室内为主"], "孩子 8 岁，想安排研学类场馆"),
        ("⛰️ 京郊徒步", 'bj6', "京郊长城徒步 2 日", "野长城 + 民宿，人少景好", 2, 2, 2800,
         ["朋友结伴"], ["户外自然", "出片率高"], "想走人少的野长城"),
    ],
    "上海市": [
        ("🎨 室内避雨", 'sh1', "上海室内看展打卡", "美术馆 + 展览，雨天也不怕", 2, 2, 4500,
         ["情侣/夫妻"], ["室内为主", "出片率高"], "想看美术馆和展览，尽量少走路"),
        ("🌃 外滩夜色", 'sh2', "外滩 + 陆家嘴 2 日", "白天武康路，晚上看外滩灯光", 2, 2, 4200,
         ["情侣/夫妻"], ["出片率高", "夜景为主"], "想看外滩夜景"),
        ("🍰 咖啡与弄堂", 'sh3', "上海咖啡漫步 2 日", "梧桐区小店、弄堂、老洋房", 2, 2, 3800,
         ["独自出行"], ["美食为主", "出片率高"], "想喝咖啡、逛小店，节奏轻松"),
        ("👶 亲子科技馆", 'sh4', "上海亲子 2 日", "科技馆 + 自然博物馆", 2, 3, 4600,
         ["带小孩"], ["亲子友好", "室内为主"], "孩子 7 岁，想去科技馆"),
        ("🎭 演出与夜游", 'sh5', "上海看剧 + 浦江夜游 2 日", "下午看剧，晚上坐船", 2, 2, 5200,
         ["情侣/夫妻"], ["室内为主", "夜景为主"], "想看一场话剧，再坐船夜游"),
        ("🥢 本帮菜小馆", 'sh6', "上海本帮菜 2 日", "老字号与本帮小馆，一天三顿", 2, 2, 3400,
         ["朋友结伴"], ["美食为主"], "主要想吃本帮菜"),
    ],
    "杭州市": [
        ("🌊 西湖经典", 'hz1', "杭州西湖 2 日", "断桥、苏堤、灵隐寺，一条不绕路", 2, 2, 3200,
         ["情侣/夫妻"], ["出片率高", "文化历史"], "第一次来杭州，主要看西湖"),
        ("🍃 龙井茶山", 'hz2', "杭州茶山慢游 2 日", "龙井村喝茶，九溪十八涧散步", 2, 2, 3000,
         ["带老人"], ["少走路/休闲游", "户外自然"], "想喝茶、走平路，别爬山"),
        ("🎋 西溪湿地", 'hz3', "西溪湿地 + 良渚 2 日", "摇橹船穿芦苇，下午看文明遗址", 2, 3, 3400,
         ["亲子家庭"], ["户外自然", "亲子友好"], "想坐船，孩子会喜欢"),
    ],
    "大理市": [
        ("🏞️ 环洱海", 'dl1', "大理环洱海 3 日", "包车环湖，一路都是取景点", 3, 2, 4200,
         ["情侣/夫妻"], ["出片率高", "户外自然"], "想环洱海，节奏轻松"),
        ("🏔️ 苍山徒步", 'dl2', "苍山 + 洱海 3 日", "上午上山，下午湖边发呆", 3, 2, 4600,
         ["朋友结伴"], ["户外自然", "出片率高"], "想走苍山步道"),
        ("☕ 古城慢生活", 'dl3', "大理古城躺平 4 日", "晒太阳、喝咖啡、逛市集", 4, 2, 5000,
         ["独自出行"], ["少走路/休闲游", "美食为主"], "不想赶行程，就想待着"),
    ],
    "西安市": [
        ("🏛️ 兵马俑必看", 'xa1', "西安经典 3 日", "兵马俑、城墙、回民街一次走完", 3, 2, 4000,
         ["情侣/夫妻"], ["文化历史", "美食为主"], "第一次来西安，想看兵马俑"),
        ("👴 慢游古都", 'xa2', "西安老人友好 3 日", "每天两景点，多坐少走", 3, 3, 4800,
         ["带老人"], ["少走路/休闲游", "文化历史"], "带父母，别太累"),
        ("🌙 大唐不夜城", 'xa3', "西安夜景 + 汉服 2 日", "大唐不夜城穿汉服，出片率高", 2, 2, 3000,
         ["朋友结伴"], ["出片率高", "夜景为主"], "想拍汉服照片，看夜景"),
    ],
    "厦门市": [
        ("🏝️ 鼓浪屿", 'xm1', "厦门鼓浪屿 3 日", "老别墅、海边、慢节奏", 3, 2, 3800,
         ["情侣/夫妻"], ["出片率高", "少走路/休闲游"], "想去鼓浪屿，节奏慢一点"),
        ("🏫 亲子研学", 'xm2', "厦门亲子海边 3 日", "沙滩 + 科技馆 + 环岛路", 3, 3, 4600,
         ["带小孩", "亲子家庭"], ["亲子友好", "户外自然"], "孩子想去海边玩沙"),
        ("🍜 闽南小吃", 'xm3', "厦门小吃深度 2 日", "沙茶面、土笋冻、八市扫街", 2, 2, 2600,
         ["朋友结伴"], ["美食为主"], "以吃为主，想逛本地市场"),
    ],
}


def _build_card(city, idx, row):
    tag, img, title, desc, days, people, budget, companions, prefs, request = row
    return {
        "key": f"{city}-{idx}",
        "tag": tag,
        "img": IMG[img],
        "title": title,
        "desc": desc,
        "payload": {
            "city": city,
            "days": days,
            "people": people,
            "budget": budget,
            "companions": companions,
            "preferences": prefs,
            "request": request,
        },
    }



# ---------------------------------------------------------------------------
# 补齐数据：每个城市凑满 6 条，保证市级联动正好 6 张卡片
# ---------------------------------------------------------------------------

EXTRA_RAW: dict[str, list[tuple]] = {'梅州市': [('🏛️ 古村探访', 'mz4', '梅州古村落 2 日', '桥溪古村 + 松口古镇，慢走细看', 2, 2, 2400, ['带老人'], ['少走路/休闲游', '文化历史'], '想看老屋和古村，路别太陡'), ('🍚 客家美食', 'mz5', '梅州客家菜 2 日', '盐焗鸡、酿豆腐、腌面一天吃遍', 2, 2, 2200, ['朋友结伴'], ['美食为主'], '以吃为主，想吃地道客家菜'), ('♨️ 温泉康养', 'mz6', '梅州温泉康养 3 日', '泡汤 + 山间散步，节奏最松', 3, 2, 3600, ['带老人', '情侣/夫妻'], ['少走路/休闲游'], '主要想泡温泉休息')], '广州市': [('🏛️ 沙面建筑', 'gz4', '广州沙面 + 永庆坊 2 日', '老洋房与骑楼，拍照不重样', 2, 2, 2900, ['情侣/夫妻'], ['出片率高', '文化历史'], '想拍老建筑'), ('🚢 珠江夜游', 'gz5', '广州夜游 + 北京路 2 日', '白天逛博物馆，晚上坐船', 2, 2, 3000, ['朋友结伴'], ['夜景为主', '美食为主'], '想看夜景，顺便吃小吃'), ('🌳 白云山轻徒步', 'gz6', '广州白云山 + 越秀 2 日', '半天上山，半天老城', 2, 3, 2600, ['亲子家庭'], ['户外自然', '亲子友好'], '想爬不难的山，孩子也能走')], '深圳市': [('🐬 亲子海洋', 'sz4', '深圳海洋亲子 2 日', '海洋馆 + 红树林，孩子放电', 2, 3, 3800, ['带小孩', '亲子家庭'], ['亲子友好', '室内为主'], '孩子想看海洋动物'), ('🏖️ 大鹏半岛', 'sz5', '深圳大鹏海边 2 日', '较场尾住一晚，早起看日出', 2, 2, 3600, ['情侣/夫妻'], ['户外自然', '出片率高'], '想住海边看日出'), ('🍜 城中美食', 'sz6', '深圳美食扫街 2 日', '从东门到蛇口，一路吃过去', 2, 2, 3000, ['朋友结伴'], ['美食为主'], '主要想吃，越本地越好')], '成都市': [('🎋 熊猫义工', 'cd4', '成都熊猫义工 2 日', '近距离喂食，孩子最爱的项目', 2, 3, 4200, ['带小孩', '亲子家庭'], ['亲子友好', '室内为主'], '孩子想看熊猫，最好能近距离'), ('🍲 川菜寻味', 'cd5', '成都川菜寻味 3 日', '苍蝇馆子到私房菜，一日三餐', 3, 2, 3400, ['朋友结伴'], ['美食为主'], '以吃为主，能吃辣'), ('🏞️ 青城后山', 'cd6', '成都青城后山 2 日', '避暑、走溪谷、住山间民宿', 2, 2, 2800, ['情侣/夫妻'], ['户外自然', '少走路/休闲游'], '想避暑，走点平缓山路')], '甘孜州': [('🌄 日出观景', 'gzz4', '川西日出观景 3 日', '子梅垭口守日出，值回票价', 3, 2, 7800, ['朋友结伴'], ['出片率高', '户外自然', '特种兵拉练'], '愿意凌晨起床看日出'), ('🏕️ 高原露营', 'gzz5', '川西高原露营 3 日', '湖边扎营，夜里看星空', 3, 2, 8200, ['朋友结伴'], ['户外自然', '特种兵拉练'], '想露营看星空'), ('🐎 草原牧歌', 'gzz6', '川西草原环线 3 日', '塔公草原骑马，慢慢晃', 3, 2, 7600, ['情侣/夫妻'], ['户外自然', '出片率高'], '想在草原上待着，不想赶')], '北京市': [('🎭 京味曲艺', 'bj4', '北京相声 + 老舍茶馆 2 日', '下午听相声，晚上喝盖碗茶', 2, 2, 3200, ['带老人'], ['文化历史', '室内为主'], '想看传统曲艺'), ('🏛️ 国博深度', 'bj5', '北京博物馆深度 3 日', '国博、首博、大运河馆', 3, 2, 4000, ['独自出行'], ['室内为主', '文化历史'], '想看博物馆，一天一个'), ('🍂 香山秋色', 'bj6', '北京香山 + 植物园 2 日', '秋天看红叶，缆车上山', 2, 2, 2600, ['情侣/夫妻'], ['户外自然', '出片率高'], '想爬山看红叶，能坐缆车')], '上海市': [('🏛️ 外滩建筑', 'sh4', '上海外滩建筑漫步 2 日', '万国建筑群 + 邬达克作品', 2, 2, 4200, ['独自出行'], ['文化历史', '出片率高'], '想认真看老建筑'), ('🛍️ 梧桐逛街', 'sh5', '上海梧桐区逛街 2 日', '安福路、武康路、愚园路', 2, 2, 4000, ['情侣/夫妻'], ['美食为主', '出片率高'], '想逛街拍照，节奏轻松'), ('🎠 亲子迪士尼', 'sh6', '上海迪士尼 2 日', '两天一园，孩子玩到闭园', 2, 3, 6800, ['带小孩', '亲子家庭'], ['亲子友好', '出片率高'], '以迪士尼为主，孩子 6 岁')], '杭州市': [('🏯 灵隐古刹', 'hz4', '杭州灵隐 + 法喜寺 2 日', '古刹与茶园，安静的一条线', 2, 2, 3000, ['带老人'], ['文化历史', '少走路/休闲游'], '想逛寺庙，路平一点'), ('🚴 环湖骑行', 'hz5', '杭州环西湖骑行 2 日', '早起绕湖一圈，中午喝茶', 2, 2, 2800, ['朋友结伴'], ['户外自然', '出片率高'], '想骑车绕西湖'), ('🎨 运河街区', 'hz6', '杭州运河 + 小河直街 2 日', '老厂房改的街区，咖啡馆多', 2, 2, 3200, ['情侣/夫妻'], ['室内为主', '出片率高'], '想逛文创街区')], '大理市': [('🏘️ 喜洲古镇', 'dl4', '大理喜洲 + 海舌 2 日', '白族民居与稻田，出片稳', 3, 2, 4000, ['情侣/夫妻'], ['出片率高', '文化历史'], '想拍白族建筑'), ('🌅 双廊日出', 'dl5', '大理双廊住两晚 3 日', '推窗就是洱海，什么都不做', 3, 2, 4800, ['情侣/夫妻'], ['少走路/休闲游', '出片率高'], '想住海景房，躺着看日出'), ('🐎 苍山茶马古道', 'dl6', '大理茶马古道 3 日', '骑马走古道，下午回古城', 3, 2, 4400, ['朋友结伴'], ['户外自然', '文化历史'], '想走茶马古道')], '西安市': [('🍜 回民街小吃', 'xa4', '西安小吃深度 2 日', '泡馍、肉夹馍、甑糕一天六顿', 2, 2, 2800, ['朋友结伴'], ['美食为主'], '以吃为主，想扫街'), ('🏛️ 陕西历史', 'xa5', '西安博物馆 2 日', '陕历博 + 碑林，一天看不完', 2, 2, 3000, ['独自出行'], ['室内为主', '文化历史'], '想看文物和碑刻'), ('🏔️ 华山论剑', 'xa6', '西安 + 华山 3 日', '一天西安，一天华山上下', 3, 2, 4200, ['朋友结伴'], ['户外自然', '特种兵拉练'], '想爬华山，体力还行')], '厦门市': [('🌊 环岛路骑行', 'xm4', '厦门环岛路 2 日', '海边骑车，傍晚看日落', 2, 2, 3200, ['情侣/夫妻'], ['户外自然', '出片率高'], '想骑车看海'), ('🏛️ 集美学村', 'xm5', '厦门集美 + 曾厝垵 2 日', '嘉庚建筑与文艺小店', 2, 2, 3000, ['独自出行'], ['文化历史', '出片率高'], '想看老建筑和逛小店'), ('🐚 亲子沙坡尾', 'xm6', '厦门亲子海边 3 日', '沙滩、科技馆、海鲜市场', 3, 3, 4400, ['带小孩', '亲子家庭'], ['亲子友好', '美食为主'], '孩子想玩沙吃海鲜')]}

for _city, _rows in EXTRA_RAW.items():
    # 只在不足 6 条时补齐，避免本来够数的城市被堆到 9 条
    _have = len(RAW.setdefault(_city, []))
    if _have < 6:
        RAW[_city].extend(_rows[: 6 - _have])


CITY_CARDS = {
    city: [_build_card(city, i, row) for i, row in enumerate(rows, start=1)]
    for city, rows in RAW.items()
}

# 全国推荐池的优先级：按运营热度排序，避免直接依赖字典顺序
CITY_PRIORITY = [
    "北京市", "上海市", "广州市", "成都市", "杭州市", "深圳市",
    "西安市", "厦门市", "大理市", "梅州市", "甘孜州",
]

# 「不限」占位项：让用户只看省级 / 全国推荐，而不用被迫选具体城市。
# 值为空串提交给 Agent 前会被拦截（Agent 要求城市必填）。
UNLIMITED_PROVINCE = "🗺️ 不限省份（看全国推荐）"
UNLIMITED_CITY = "🏙️ 不限城市（看本省推荐）"


def _interleave(groups: list[list[Any]], limit: int) -> list[Any]:
    """跨城市轮流取：先各城第 1 个，再各城第 2 个……避免整屏同一个城市。"""
    picked: list[Any] = []
    depth = 0
    while len(picked) < limit:
        added = False
        for group in groups:
            if depth < len(group):
                picked.append(group[depth])
                added = True
                if len(picked) >= limit:
                    return picked
        if not added:
            break
        depth += 1
    return picked


def pick_recommendations(limit: int = 6) -> tuple[list[dict[str, Any]], str]:
    """按当前目的地级联挑推荐：市级 > 省级 > 全国，最多 limit 张。

    只读 session_state，不写状态，可以安全地在控件渲染之后调用。
    """
    country = st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"])
    province = str(st.session_state.get("f_province", "") or "")
    city = str(st.session_state.get("f_city", "") or "")

    if city and city != UNLIMITED_CITY and city in CITY_CARDS:
        return CITY_CARDS[city][:limit], f"{city} · 为你精选"

    if province and province != UNLIMITED_PROVINCE:
        groups = [
            CITY_CARDS[one]
            for one in DESTINATION_TREE.get(country, {}).get(province, [])
            if one in CITY_CARDS
        ]
        if groups:
            return _interleave(groups, limit), f"{province} · 热门行程"

    groups = [CITY_CARDS[one] for one in CITY_PRIORITY if one in CITY_CARDS]
    return _interleave(groups, limit), "全国热门推荐"


# ---------------------------------------------------------------------------
# 全屏动态背景：多张高清图交叉淡入淡出，随目的地三级联动换图组
# ---------------------------------------------------------------------------
# 图源全部复用 IMG 里已按行程文案校对过的 Unsplash 直链，只从中挑「适合铺满整屏」
# 的外景（地标 / 山水 / 海岸），不新增未校对的 photo id，避免出现「选成都、放罗马」
# 这类错配，也不会因为图链失效而露出底色。
BG_SLIDES = 4                  # 每组背景轮播几张
BG_HOLD = 9.0                  # 单张占位秒数（含淡入淡出），4 张约 36 秒一轮
BG_FADE = 3.0                  # 相邻两张交叉淡化的秒数
BG_ZOOM = (1.03, 1.09)         # 每张在本窗口内的轻微推近，模拟镜头流动感

# 城市 -> 背景用图（IMG 的 key），顺序即轮播顺序。
# 逐张核对过实际画面：只留外景 / 地标，室内展陈与美食特写不进背景组，
# 文案与画面对不上的（mz2 湖心亭、gz6 草原、xa2 海滨天际线等）也一并剔除。
BG_CITY_KEYS: dict[str, list[str]] = {
    "梅州市": ["mz1", "mz3", "mz4"],              # 老墙 / 茶田 / 灯笼古街
    "广州市": ["gz2", "gz5", "gz4"],              # 广州塔 / 珠江夜景 / 沙面洋房
    "深圳市": ["sz1", "sz5", "sz2"],              # 深圳湾 / 大鹏海岸 / 创意园
    "成都市": ["cd1", "cd3", "cd2", "cd6"],        # 熊猫 / 都江堰 / 锦里 / 青城后山
    "甘孜州": ["gzz1", "gzz3", "gzz4", "gzz6"],    # 川西公路 / 雪山 / 日出 / 草原
    "北京市": ["bj2", "bj1", "bj6", "bj3"],        # 长城 / 故宫 / 京郊野长城 / 胡同
    "上海市": ["sh2", "sh4", "sh1", "sh5"],        # 外滩夜景 / 装置 / 美术馆 / 剧场
    "杭州市": ["hz1", "hz2", "hz3", "hz4"],        # 西湖 / 龙井 / 西溪 / 灵隐
    "大理市": ["dl1", "dl3", "dl2", "dl5"],        # 洱海 / 古城街市 / 苍山 / 双廊
    "西安市": ["xa1", "xa3", "xa6"],               # 兵马俑 / 古都石狮 / 华山
    "厦门市": ["xm1", "xm4", "xm2", "xm5"],        # 厦门湾 / 环岛路 / 学村 / 骑楼
}

# 全国池：不按城市顺序轮播，直接挑四张最有「大好河山」辨识度的外景打底
BG_NATIONAL_KEYS = ["bj2", "hz1", "dl1", "gzz1"]  # 长城 / 西湖 / 洱海 / 川西


def _bg_url(key: str) -> str:
    """卡片图是 900 宽，背景要铺满整屏，同一张图升到 1920 宽。"""
    return IMG[key].replace("w=900", "w=1920")


def _city_bg_keys(city: str) -> list[str]:
    """该城的背景候选：优先用校对过的外景表，没收录就退回卡片图顺序。"""
    if city in BG_CITY_KEYS:
        return BG_CITY_KEYS[city]
    return [row[1] for row in RAW.get(city, [])]


def _bg_group() -> tuple[list[str], str]:
    """按 城市 -> 省份 -> 全国 逐级挑背景，返回 (IMG key 列表, 范围名)。

    「其他国家 / 地区」还没有校对过的图源，暂时落到全国池；补上 IMG 后自动生效。
    """
    country = st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"])
    province = str(st.session_state.get("f_province", "") or "")
    city = str(st.session_state.get("f_city", "") or "")

    if city and city != UNLIMITED_CITY:
        keys = _city_bg_keys(city)
        if keys:
            return keys[:BG_SLIDES], city

    if province and province != UNLIMITED_PROVINCE:
        groups = [
            g
            for g in (
                _city_bg_keys(one)
                for one in DESTINATION_TREE.get(country, {}).get(province, [])
            )
            if g
        ]
        if groups:
            return _interleave(groups, BG_SLIDES), province

    # 全国：直接用策划好的地标池，首屏就是「大好河山」，而不是某个城市的室内展
    return BG_NATIONAL_KEYS, "全国"


def _bg_alpha(t: float, center: float, cycle: float) -> float:
    """第 t 秒这张图的可见度：窗口内保持 1，两端各用半个 BG_FADE 与邻居交叉淡化。"""
    dist = abs((t - center + cycle / 2.0) % cycle - cycle / 2.0)
    knee, edge = (BG_HOLD - BG_FADE) / 2.0, (BG_HOLD + BG_FADE) / 2.0
    if dist <= knee:
        return 1.0
    if dist >= edge:
        return 0.0
    return (edge - dist) / BG_FADE


def _bg_scale(t: float, center: float, cycle: float) -> float:
    """可见窗口内从 BG_ZOOM[0] 匀速推到 [1]；回跳只发生在完全透明的那段。"""
    span = BG_HOLD + BG_FADE
    ratio = ((t - center + span / 2.0) % cycle) / span
    lo, hi = BG_ZOOM
    return lo + (hi - lo) * min(ratio, 1.0)


def _bg_keyframes(idx: int, total: int) -> str:
    """生成 tvbgN 关键帧。

    断点只取折线的拐点，浏览器线性插值即可还原整条曲线；时间按周期取模，
    首尾两张的淡化窗口跨 0 秒边界时会自动回绕，循环处不会出现黑帧或跳切。
    """
    cycle = BG_HOLD * total
    # 窗口中心落在 idx * BG_HOLD：首屏第 0 张正好处于全显平台期，不会以半透明开场
    center = idx * BG_HOLD
    edge, knee = (BG_HOLD + BG_FADE) / 2.0, (BG_HOLD - BG_FADE) / 2.0
    times = {0.0, cycle}
    for delta in (-edge, -knee, knee, edge):
        times.add((center + delta) % cycle)
    stops = []
    for t in sorted(times):
        stops.append(
            f"{t / cycle * 100:.2f}%{{opacity:{_bg_alpha(t, center, cycle):.3f};"
            f"transform:scale({_bg_scale(t, center, cycle):.4f})}}"
        )
    body = " ".join(stops)
    return f"@keyframes tvbg{idx}{{{body}}}"


def background_html() -> str:
    """背景层 HTML。

    内容只随目的地级联变化：同一次选择下重复 rerun，这段字符串逐字节相同，
    Streamlit 不会重发该元素，动画因此不会被重置回第一张。
    """
    keys, scope = _bg_group()
    total = len(keys)
    cycle = BG_HOLD * total
    frames, layers = [], []
    for i, key in enumerate(keys):
        anim = f"tvbg{i} {cycle:.1f}s ease-in-out infinite" if total > 1 else "none"
        url = escape(_bg_url(key), quote=True)
        layers.append(
            f'<div class="tv-bg-img" style="background-image:url(&#x27;{url}&#x27;);'
            f'animation:{anim};"></div>'
        )
        if total > 1:
            frames.append(_bg_keyframes(i, total))
    # 结果页要长时间读表格文字，背景再压一档；空状态页保持原亮度展示风光
    mode = "result" if st.session_state.get("result") else "form"
    return (
        f'<div class="tv-bg" aria-hidden="true" data-mode="{mode}" '
        f'data-scope="{escape(scope, quote=True)}">'
        f'{"".join(layers)}<div class="tv-bg-mask"></div></div>'
        f'<style>{"".join(frames)}</style>'
    )





# ---------------------------------------------------------------------------
# 全局 CSS（任务 5）：深灰蓝底 + 毛玻璃 + 渐变按钮 + 时间轴
# ---------------------------------------------------------------------------
GLOBAL_CSS = """
<style>
/* 1. 全局背景：交给下方「维度一」的主题背景统一接管，这里只定文字色 */
.stApp {
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
/* 主题背景：原「航线图」矢量纹理下线，画面交给页面最底层的 .tv-bg 图片轮播。
   这里只留 html/body 的深色底：图片加载前后都不闪白，也是遮罩之外的最终底色。 */
html, body {
    background: #070B14 !important;
}
.stApp,
[data-testid="stAppViewContainer"],
[data-testid="stMain"] {
    background: transparent !important;
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
    font-size: 12px;
    color: #6B7A90;
    opacity: 0.55;
    line-height: 1.85;
    margin: 14px auto 0 auto;
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

/* ---------- 三、收敛光污染：不再压平背景，航线纹理统一由上方 .stApp 控制 ---------- */
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
    color: #64748B !important;
    opacity: 0.55;
    font-size: 12px;
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


/* ==========================================================================
   V4 图文示例卡片（小红书/携程宣传图风格）
   ========================================================================== */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) {
    padding: 0 !important;
    background: #0B1220 !important;
    border: none !important;
    border-radius: 16px !important;
    overflow: hidden !important;
    box-shadow: 0 8px 24px rgba(2, 6, 23, 0.45);
    transition: transform 0.3s ease, box-shadow 0.3s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) > div,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) [data-testid="stVerticalBlock"] {
    gap: 0 !important;
    padding: 0 !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card):hover {
    transform: translateY(-5px);
    box-shadow: 0 22px 44px rgba(2, 6, 23, 0.68);
}

.pc-card {
    position: relative;
    height: 240px;
    overflow: hidden;
    border-radius: 16px 16px 0 0;
    background: #1E293B;
    /* 入场动画：每次目的地切换、卡片内容被重新渲染时都会重新播放 */
    animation: cardRiseIn 0.4s ease-out both;
    will-change: transform, opacity;
}
@keyframes cardRiseIn {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
}
.pc-img {
    position: absolute;
    inset: 0;
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    transform: scale(1);
    transition: transform 0.3s ease;
}
.pc-card:hover .pc-img,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card):hover .pc-img {
    transform: scale(1.05);
}
.pc-shade {
    position: absolute;
    inset: 0;
    background: linear-gradient(to top,
                rgba(0, 0, 0, 0.90) 0%,
                rgba(0, 0, 0, 0.45) 38%,
                rgba(0, 0, 0, 0.00) 72%);
    pointer-events: none;
}
.pc-tag {
    position: absolute;
    top: 12px;
    left: 12px;
    padding: 4px 11px;
    font-size: 12px;
    font-weight: 700;
    line-height: 1.45;
    color: #FFFFFF;
    background: rgba(15, 23, 42, 0.60);
    border: 1px solid rgba(255, 255, 255, 0.22);
    border-radius: 999px;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}
.pc-body {
    position: absolute;
    left: 14px;
    right: 14px;
    bottom: 12px;
}
.pc-title {
    font-size: 17px;
    font-weight: 800;
    line-height: 1.35;
    color: #FFFFFF;
    text-shadow: 0 2px 10px rgba(0, 0, 0, 0.60);
}
.pc-desc {
    margin-top: 5px;
    font-size: 12.5px;
    font-weight: 400;
    line-height: 1.55;
    color: #D3DCE8;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.55);
}

/* 按钮与图片融合为一条底部操作栏 */
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) .stButton {
    margin: 0 !important;
    padding: 0 !important;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) .stButton > button,
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) .stButton > button[kind="secondary"] {
    width: 100% !important;
    margin: 0 !important;
    padding: 0.62rem 1rem;
    background: #0B1220 !important;
    border: none !important;
    border-top: 1px solid rgba(148, 163, 184, 0.14) !important;
    border-radius: 0 0 16px 16px !important;
    color: #60A5FA !important;
    font-size: 13.5px;
    font-weight: 700;
    box-shadow: none !important;
    transition: background 0.2s ease, color 0.2s ease;
}
div[data-testid="stVerticalBlockBorderWrapper"]:has(.pc-card) .stButton > button:hover {
    background: rgba(59, 130, 246, 0.18) !important;
    color: #BFDBFE !important;
    border-top-color: rgba(59, 130, 246, 0.40) !important;
    transform: none;
    box-shadow: none !important;
}

/* ---------- 推荐区标题（随目的地联动的范围提示） ---------- */
.rec-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    margin: 4px 2px 14px 2px;
}
.rec-scope {
    font-size: 15px;
    font-weight: 800;
    color: #E2E9F5;
    letter-spacing: 0.4px;
}
.rec-scope::before {
    content: "";
    display: inline-block;
    width: 3px;
    height: 14px;
    margin-right: 8px;
    border-radius: 2px;
    background: linear-gradient(180deg, #3B82F6, #8B5CF6);
    vertical-align: -1px;
}
.rec-tip {
    font-size: 12px;
    color: #7C8CA3;
}

/* ==========================================================================
   全屏动态背景：图片交叉淡入淡出 + 暗色遮罩
   .tv-bg / .tv-bg-img 由 background_html() 按目的地级联生成，
   每张图的动画名 tvbgN 与时长写在元素内联 style 上，这里只定公共样式。
   ========================================================================== */
.tv-bg {
    position: fixed;
    inset: 0;
    z-index: -1;                       /* 沉到所有内容之下、html 深色底之上 */
    overflow: hidden;
    pointer-events: none;
}
.tv-bg-img {
    position: absolute;
    inset: 0;
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    will-change: opacity, transform;
}
/* 遮罩：整体压暗 + 四角渐深，再叠原品牌的蓝紫径向光晕与噪点，保证文字与卡片可读 */
.tv-bg-mask {
    position: absolute;
    inset: 0;
    background:
        linear-gradient(180deg, rgba(7, 11, 20, 0.82) 0%, rgba(7, 11, 20, 0.66) 42%, rgba(7, 11, 20, 0.88) 100%),
        radial-gradient(120% 88% at 50% 38%, rgba(7, 11, 20, 0) 28%, rgba(4, 7, 15, 0.72) 100%),
        radial-gradient(1100px 560px at 12% -10%, rgba(56, 189, 248, 0.12), transparent 62%),
        radial-gradient(950px 520px at 88% -4%, rgba(129, 90, 246, 0.14), transparent 58%),
        url("data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyMDAiIGhlaWdodD0iMjAwIj48ZmlsdGVyIGlkPSJuIj48ZmVUdXJidWxlbmNlIHR5cGU9ImZyYWN0YWxOb2lzZSIgYmFzZUZyZXF1ZW5jeT0iMC44NSIgbnVtT2N0YXZlcz0iMyIgc3RpdGNoVGlsZXM9InN0aXRjaCIvPjxmZUNvbG9yTWF0cml4IHR5cGU9InNhdHVyYXRlIiB2YWx1ZXM9IjAiLz48L2ZpbHRlcj48cmVjdCB3aWR0aD0iMjAwIiBoZWlnaHQ9IjIwMCIgZmlsdGVyPSJ1cmwoI24pIiBvcGFjaXR5PSIwLjA1NSIvPjwvc3ZnPg==");
    background-repeat: no-repeat, no-repeat, no-repeat, no-repeat, repeat;
    background-size: auto, auto, auto, auto, 200px 200px;
}
/* 背景换成实景照片后，小字需要一层投影才压得住亮色画面（如成都茶灯笼那张） */
.hero-title, .hero-sub, .rec-scope, .rec-tip {
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.85);
}
/* 行程结果页：背景只当氛围，压暗降饱和，避免长时间读表格时被轮播抢注意力 */
.tv-bg[data-mode="result"] .tv-bg-img {
    filter: brightness(0.55) saturate(0.85);
}
/* 侧边栏改为半透明：背景的流动感能透出来，blur + 高墨底仍保证表单对比度 */
section[data-testid="stSidebar"] {
    background: rgba(11, 17, 30, 0.78) !important;
    backdrop-filter: blur(18px) saturate(115%);
}
/* 用户偏好「减少动态效果」时，只保留当前图组的第一张，停掉轮播与推近 */
@media (prefers-reduced-motion: reduce) {
    .tv-bg-img { animation: none !important; opacity: 0; }
    .tv-bg-img:first-child { opacity: 1; }
}
</style>
"""


def inject_css() -> None:
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
    # 背景层紧跟 CSS 之后：它是正文第一个元素，内容不变时 rerun 不会重发，动画不被打断
    st.markdown(background_html(), unsafe_allow_html=True)


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
    """首屏视觉中心：Agent 身份 + 随目的地联动的图文推荐卡片。"""
    cards, scope = pick_recommendations(6)

    st.markdown(
        "<div class='hero'>"
        "<div class='hero-title'>✈️ 旅游规划助手</div>"
        "<div class='hero-sub'>我是您的贴心旅行管家，请告诉我您的需求，"
        "或者直接选中意的行程一键套用，左侧条件我会同步填好。</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        f"<div class='rec-head'><span class='rec-scope'>{escape(scope)}</span>"
        "<span class='rec-tip'>点开卡片下方按钮即可套用</span></div>",
        unsafe_allow_html=True,
    )

    for row_idx, row in enumerate((cards[0:3], cards[3:6])):
        if not row:
            continue
        cols = st.columns(3, gap="medium")
        for col_idx, (col, preset) in enumerate(zip(cols, row)):
            with col:
                with card():
                    # 同一排的卡片依次延迟 60ms 出现，形成「依次飘上来」的错落感
                    delay = (row_idx * 3 + col_idx) * 60
                    st.markdown(
                        f"<div class='pc-card' style='animation-delay:{delay}ms'>"
                        f"<div class='pc-img' style=\"background-image:url('{preset['img']}')\"></div>"
                        "<div class='pc-shade'></div>"
                        f"<div class='pc-tag'>{escape(preset['tag'])}</div>"
                        "<div class='pc-body'>"
                        f"<div class='pc-title'>{escape(preset['title'])}</div>"
                        f"<div class='pc-desc'>{escape(preset['desc'])}</div>"
                        "</div>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
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
    # 初始不预设省市：首屏展示全国热门推荐，用户选到哪一层就联动到哪一层
    "province": UNLIMITED_PROVINCE,
    "city": UNLIMITED_CITY,
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

    province_options = [UNLIMITED_PROVINCE] + list(DESTINATION_TREE.get(country, {}))
    _sync_select(k("province"), province_options, base.get("province"))
    province = st.selectbox("省份 / 直辖市", province_options, key=k("province"))

    if province == UNLIMITED_PROVINCE:
        city_options = [UNLIMITED_CITY]
    else:
        city_options = [UNLIMITED_CITY] + list(
            DESTINATION_TREE.get(country, {}).get(province, [])
        )
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
        "province": "" if province == UNLIMITED_PROVINCE else province,
        "city": "" if city == UNLIMITED_CITY else city,
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

    if run_clicked and not str(payload.get("city") or "").strip():
        st.warning("请先在左侧把「城市」选成具体城市，再开始规划。")
        run_clicked = False

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
