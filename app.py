"""Streamlit 界面（深色 / 毛玻璃 / 卡片式时间轴 UI 升级版）。

流程：左侧约束 -> st.status 上帝视角 -> 卡片式时间轴行程 -> 指标与行程单下载。
运行：streamlit run app.py

只负责显示：所有中英文对照来自 travel_agent.zh_labels，
原始参数 / 坐标 / 毫秒耗时写后台日志（logs/trace-detail.log），不上屏。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as st_components

from travel_agent import agent, config, llm, pdf_export, zh_labels

from components.map_intro import map_intro

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


def _itinerary_pdf_bytes(result: dict[str, Any]) -> bytes:
    """生成行程单 PDF 并缓存在会话里：翻页/重跑不重复下载景点实拍图。"""
    signature = hashlib.md5(
        json.dumps(
            {
                "markdown": result.get("markdown") or "",
                "city": (result.get("constraints") or {}).get("city"),
                "days": (result.get("constraints") or {}).get("days"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    cached = st.session_state.get("_pdf_cache") or {}
    if cached.get("signature") == signature and cached.get("data"):
        return cached["data"]
    data = pdf_export.build_itinerary_pdf(result)
    st.session_state["_pdf_cache"] = {"signature": signature, "data": data}
    return data


def full_width_table(rows: Any, **kwargs: Any) -> None:
    """满宽表格：新版 width="stretch"，老版 use_container_width=True。"""
    try:
        st.dataframe(rows, width="stretch", **kwargs)
    except TypeError:
        st.dataframe(rows, use_container_width=True, **kwargs)


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
        "stops": CARD_STOPS.get(f"{city}-{idx}"),
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


# 每张卡的主点，按卡片 key 存：desc 是给眼睛看的一句话，抠不出可靠的地名
# （「颐和园一次走完」这种整句评价，剪尾巴会剪出碎片，整段丢又赔掉真地点），
# 所以这里显式写一遍。美食/温泉/躺平主题的卡本来就没什么地名可写，列的是它主打的东西。
# 空列表是故意的：那张卡确实没说去哪，详情里就走「主点等规划时逐日排」。
CARD_STOPS: dict[str, list[str]] = {
    # 梅州
    "梅州市-1": ["围龙屋", "客家博物馆"],
    "梅州市-2": ["围龙屋", "客家博物馆"],
    "梅州市-3": ["雁南飞茶田", "温泉"],
    "梅州市-4": ["桥溪古村", "松口古镇"],
    "梅州市-5": ["盐焗鸡", "酿豆腐", "腌面"],
    "梅州市-6": ["温泉泡汤", "山间散步"],
    # 广州
    "广州市-1": ["陶陶居", "上下九"],
    "广州市-2": ["沙面", "小蛮腰", "珠江夜游"],
    "广州市-3": ["野生动物世界", "欢乐世界"],
    "广州市-4": ["沙面", "永庆坊"],
    "广州市-5": ["北京路", "珠江夜游"],
    "广州市-6": ["白云山", "越秀老城"],
    # 深圳
    "深圳市-1": ["深圳湾", "大梅沙"],
    "深圳市-2": ["华侨城创意园", "园区咖啡馆"],
    "深圳市-3": ["科技馆", "人才公园"],
    "深圳市-4": ["海洋馆", "红树林"],
    "深圳市-5": ["较场尾", "海边日出"],
    "深圳市-6": ["东门", "蛇口"],
    # 成都
    "成都市-1": ["熊猫基地", "老茶馆", "火锅"],
    "成都市-2": ["宽窄巷子", "人民公园", "锦里"],
    "成都市-3": ["都江堰", "青城山"],
    "成都市-4": ["熊猫基地", "义工体验"],
    "成都市-5": ["苍蝇馆子", "私房菜"],
    "成都市-6": ["青城后山", "溪谷", "山间民宿"],
    # 甘孜
    "甘孜州-1": ["高原观景点", "盘山公路"],
    "甘孜州-2": ["新都桥", "摄影取景点"],
    "甘孜州-3": ["雪山", "牛奶海"],
    "甘孜州-4": ["子梅垭口"],
    "甘孜州-5": ["湖边营地", "星空"],
    "甘孜州-6": ["塔公草原"],
    # 北京
    "北京市-1": ["故宫", "长城", "颐和园"],
    "北京市-2": [],
    "北京市-3": ["798", "国博", "南锣鼓巷"],
    "北京市-4": ["豆汁", "卤煮", "烤鸭"],
    "北京市-5": ["科技馆", "天文馆", "动物园"],
    "北京市-6": ["野长城", "山村民宿"],
    # 上海
    "上海市-1": ["美术馆", "展览"],
    "上海市-2": ["武康路", "外滩", "陆家嘴"],
    "上海市-3": ["梧桐区", "弄堂", "老洋房"],
    "上海市-4": ["科技馆", "自然博物馆"],
    "上海市-5": ["剧场", "浦江夜游"],
    "上海市-6": ["老字号", "本帮小馆"],
    # 杭州
    "杭州市-1": ["断桥", "苏堤", "灵隐寺"],
    "杭州市-2": ["龙井村", "九溪十八涧"],
    "杭州市-3": ["西溪湿地", "良渚"],
    "杭州市-4": ["灵隐寺", "法喜寺"],
    "杭州市-5": ["西湖"],
    "杭州市-6": ["运河", "小河直街"],
    # 大理
    "大理市-1": ["洱海"],
    "大理市-2": ["苍山", "洱海"],
    "大理市-3": ["大理古城", "市集"],
    "大理市-4": ["喜洲古镇", "海舌", "白族民居"],
    "大理市-5": ["双廊", "洱海"],
    "大理市-6": ["茶马古道", "大理古城"],
    # 西安
    "西安市-1": ["兵马俑", "城墙", "回民街"],
    "西安市-2": [],
    "西安市-3": ["大唐不夜城"],
    "西安市-4": ["泡馍", "肉夹馍", "甑糕"],
    "西安市-5": ["陕历博", "碑林"],
    "西安市-6": ["华山"],
    # 厦门
    "厦门市-1": ["鼓浪屿", "老别墅"],
    "厦门市-2": ["沙滩", "科技馆", "环岛路"],
    "厦门市-3": ["沙茶面", "土笋冻", "八市"],
    "厦门市-4": ["环岛路", "海边"],
    "厦门市-5": ["集美", "曾厝垵"],
    "厦门市-6": ["沙坡尾", "海鲜市场"],
}


CITY_CARDS = {
    city: [_build_card(city, i, row) for i, row in enumerate(rows, start=1)]
    for city, rows in RAW.items()
}

# 全国推荐池的优先级：按运营热度排序，避免直接依赖字典顺序
CITY_PRIORITY = [
    "北京市", "上海市", "广州市", "成都市", "杭州市", "深圳市",
    "西安市", "厦门市", "大理市", "梅州市", "甘孜州",
]

# 一页 8 张：桌面 4 列正好两行，整页高度落在「视口 - 吸底输入卡」里。
# 翻页交给前端的纵向 scroll snap：所有页同时在 DOM 里，页数按 ceil(总数/8) 动态算。
# 推荐池取 72 的容量，现有 11 个城市 × 6 条正好 66 张，落在 50–100 的要求区间。
PAGE_SLOTS = 8
CARD_SLOTS = 72
# 兼容旧用例的只读常量：实际页数在 render_empty_state() 里按 ceil(卡片数/8) 现算，
# 全国默认池 66 张时正好 9 页。翻页本身已交给前端 scroll snap，不再读这个值。
PAGE_COUNT = math.ceil(CARD_SLOTS / PAGE_SLOTS)

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


def _dedupe_cards(groups: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for item in group:
            if item["key"] not in seen:
                seen.add(item["key"])
                out.append(item)
    return out


def _fill(pool: list[dict[str, Any]], groups: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """先保已有顺序，再用给定分组凑满 limit 张；跨组重复的同一行程会被跳过。"""
    if len(pool) >= limit:
        return pool[:limit]
    return _dedupe_cards([pool, _interleave(groups, limit)])[:limit]


def _national_groups() -> list[list[dict[str, Any]]]:
    return [CITY_CARDS[one] for one in CITY_PRIORITY if one in CITY_CARDS]


def pick_recommendations(limit: int = CARD_SLOTS) -> tuple[list[dict[str, Any]], str]:
    """按当前目的地级联挑推荐：市级 > 省级 > 全国，尽量凑满 limit 张。

    只读 session_state，不写状态，可以安全地在控件渲染之后调用。
    市级数据每城只有 6 条，不足 limit 时先用同省、再用全国热门补齐：
    列数交给 CSS，卡片数必须是常用列数的整数倍，尾行才不会瘸腿。
    """
    country = st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"])
    province = str(st.session_state.get("f_province", "") or "")
    city = str(st.session_state.get("f_city", "") or "")

    if city and city != UNLIMITED_CITY and city in CITY_CARDS:
        own = CITY_CARDS[city][:limit]
        pool = own
        if len(own) < limit:
            nearby = [
                CITY_CARDS[one]
                for one in DESTINATION_TREE.get(country, {}).get(province, [])
                if one in CITY_CARDS and one != city
            ]
            pool = _fill(own, nearby, limit)
        extra = len(pool) - len(own)
        pool = _fill(pool, _national_groups(), limit)
        scope = f"{city} · 为你精选" + ("＋顺路推荐" if extra or len(pool) > len(own) else "")
        return pool, scope

    if province and province != UNLIMITED_PROVINCE:
        groups = [
            CITY_CARDS[one]
            for one in DESTINATION_TREE.get(country, {}).get(province, [])
            if one in CITY_CARDS
        ]
        if groups:
            return _fill(_interleave(groups, limit), _national_groups(), limit), f"{province} · 热门行程"

    return _interleave(_national_groups(), limit), "全国热门推荐"


# ---------------------------------------------------------------------------
# 全屏动态背景：多张高清图交叉淡入淡出，随目的地三级联动换图组
# ---------------------------------------------------------------------------
# 注入分两段，这是「切换时渐变、rerun 时不打断」的关键：
#   骨架 background_layers_html() 字节恒定（两组图层 × 每组四张），内容不变时
#     Streamlit 不重发该元素，图层不销毁重建，轮播的动画时钟就一直走着；
#   样式 background_style_html() 只随图组 / 页面模式变化，换目的地时把新图写进
#     另一组图层并淡入，旧组保持原样淡出，两组之间是渐变过渡而不是硬切。
# 图源 bg_library.json：目的地树里每个省、每个市各配 4 张 Unsplash 免版权外景，
# 取自该地名的搜索结果，已剔除竖构图、Unsplash+ 付费图和街拍 / 美食特写。
BG_SLIDES = 4                  # 每组轮播几张，骨架里的图层数固定为这个值
BG_HOLD = 9.0                  # 单张占位秒数（含淡入淡出），4 张约 36 秒一轮
BG_FADE = 3.0                  # 组内相邻两张交叉淡化的秒数
BG_ZOOM = (1.03, 1.09)         # 每张在本窗口内的轻微推近，模拟镜头流动感
BG_CROSS = 1.8                 # 换图组时两组之间互相淡化的秒数
BG_CROSS_LAG = 0.7             # 旧图组多顶这么久再开始淡出，给新图留出下载时间
BG_STAGES = 2                  # 交替使用的两组图层：一组淡出的同时另一组淡入
BG_CYCLE = BG_HOLD * BG_SLIDES # 周期固定，关键帧与图组无关，可以写死在全局 CSS 里

# 城市 -> 背景用图（IMG 的 key），顺序即轮播顺序。
# 这 11 座城的图是按行程文案逐张核对过的（mz2 湖心亭、gz6 草原、xa2 海滨天际线
# 这类图文不符的都已剔除），优先级高于 bg_library.json，让卡片和背景说的是同一件事。
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


def _load_bg_library() -> dict[str, list[str]]:
    """读入「目的地 -> 4 个 Unsplash photo id」图库；文件缺失或读坏就退回内置表。"""
    path = Path(__file__).resolve().parent / "bg_library.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        name: [pid for pid in photos if isinstance(pid, str)]
        for name, photos in data.items()
        if isinstance(photos, list) and photos
    }


BG_LIBRARY = _load_bg_library()


def _load_video_library() -> dict[str, dict[str, str]]:
    """目的地 -> B 站视频（bv / title / author）；文件缺失就当没有，详情面板退回站内搜索链接。"""
    path = Path(__file__).resolve().parent / "video_library.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        name: item for name, item in data.items()
        if isinstance(item, dict) and str(item.get("bv") or "").startswith("BV")
    }


VIDEO_LIBRARY = _load_video_library()


def _bg_url(key: str) -> str:
    """卡片图是 900 宽，背景要铺满整屏，同一张图升到 1920 宽。"""
    return IMG[key].replace("w=900", "w=1920")


def _bg_photo_url(photo: str) -> str:
    """bg_library.json 里存的是 photo id，拼成铺满整屏的直链。"""
    return f"https://images.unsplash.com/{photo}?q=80&w=1920&auto=format&fit=crop"


def _bg_urls(name: str) -> list[str]:
    """某个省 / 市的专属图组：内置校对表 > 图库 > 该城卡片外景。"""
    if name in BG_CITY_KEYS:
        return [_bg_url(k) for k in BG_CITY_KEYS[name]]
    if name in BG_LIBRARY:
        return [_bg_photo_url(p) for p in BG_LIBRARY[name]]
    if name in RAW:
        return [_bg_url(row[1]) for row in RAW[name]]
    return []


def _bg_pad(urls: list[str], extra: list[str]) -> list[str]:
    """骨架固定 BG_SLIDES 个图层：不足 BG_SLIDES 张时先用上层（省 / 全国）的图补，
    补不齐才重复本组自己的图，保证任何时刻都有画面、组与组之间也不会撞图。"""
    pool: list[str] = []
    for u in urls + extra:
        if len(pool) >= BG_SLIDES:
            break
        if u not in pool:
            pool.append(u)
    while len(pool) < BG_SLIDES and urls:
        pool.append(urls[len(pool) % len(urls)])
    return pool


def _bg_mix(country: str, province: str) -> list[str]:
    """图库没收录这个省时，把省内各城轮着拼一份，画面至少还属于本省。"""
    groups = [g for g in (_bg_urls(one)
              for one in DESTINATION_TREE.get(country, {}).get(province, [])) if g]
    return _interleave(groups, BG_SLIDES * 2) if groups else []


def _bg_group() -> tuple[list[str], str]:
    """按 城市 -> 省份 -> 全国 逐级取专属背景，返回 (图组 URL 列表, 范围名)。"""
    country = st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"])
    province = str(st.session_state.get("f_province", "") or "")
    city = str(st.session_state.get("f_city", "") or "")
    national = [_bg_url(k) for k in BG_NATIONAL_KEYS]
    in_prov = province and province != UNLIMITED_PROVINCE
    prov_urls = _bg_urls(province) if in_prov else []
    prov_mix = _bg_mix(country, province) if in_prov else []
    province_pool = _bg_pad(prov_urls or prov_mix, national) if in_prov else []

    if city and city != UNLIMITED_CITY:
        urls = _bg_urls(city)
        if urls:
            return _bg_pad(urls, province_pool + national), city

    if in_prov:
        return province_pool, province

    # 全国：直接用策划好的地标池，首屏就是「大好河山」，而不是某个城市的室内展
    return national, "全国"


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


def _bg_keyframes(idx: int) -> str:
    """生成 tvbgN 关键帧。

    断点只取折线的拐点，浏览器线性插值即可还原整条曲线；时间按周期取模，
    首尾两张的淡化窗口跨 0 秒边界时会自动回绕，循环处不会出现黑帧或跳切。
    """
    cycle = BG_CYCLE
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


def _bg_layer_css() -> str:
    """两组共用的图层动画。周期与曲线都不依赖图组，随全局 CSS 注入一次即可。"""
    stage = f".tv-bg-stage{{transition:opacity {BG_CROSS:.1f}s ease-in-out;}}"
    runs = "".join(
        f".tv-bg-s{i}{{animation:tvbg{i} {BG_CYCLE:.0f}s ease-in-out infinite;}}"
        for i in range(BG_SLIDES)
    )
    return stage + runs + "".join(_bg_keyframes(i) for i in range(BG_SLIDES))


def background_layers_html() -> str:
    """背景骨架：BG_STAGES 组 × BG_SLIDES 张图层，字节恒定。

    这段文本永远不变，Streamlit 不会重发该元素，图层不被销毁重建，
    轮播动画和组间淡化的时钟跨每一次 rerun 连续走，地址与透明度交给样式块。
    """
    imgs = "".join(f'<div class="tv-bg-img tv-bg-s{i}"></div>' for i in range(BG_SLIDES))
    stages = "".join(
        f'<div class="tv-bg-stage" data-stage="{s}">{imgs}</div>' for s in range(BG_STAGES)
    )
    return f'<div class="tv-bg" aria-hidden="true">{stages}<div class="tv-bg-mask"></div></div>'


def background_style_html() -> str:
    """当前图组落在哪一组、两组怎么交替淡化：这段只随图组 / 页面模式变化。

    换组时只改写「另一组」的地址和相位，正在淡出的那组逐字节不变，
    浏览器就不会重置它的动画——淡出的仍是它此刻正显示的那一张。
    """
    urls, scope = _bg_group()
    sig = "|".join(urls)
    mode = "result" if st.session_state.get("result") else "form"

    if st.session_state.get("bg_sig") != sig:
        # 首屏落在第 0 组，之后每次换组都写到另一组上
        stage = 0 if st.session_state.get("bg_sig") is None else 1 - int(st.session_state.get("bg_stage", 0))
        st.session_state[f"bg_urls{stage}"] = urls
        st.session_state[f"bg_seq{stage}"] = int(st.session_state.get(f"bg_seq{stage}", 0)) + 1
        st.session_state["bg_stage"] = stage
        st.session_state["bg_sig"] = sig
    stage = int(st.session_state.get("bg_stage", 0))
    st.session_state["bg_mode"] = mode

    rules = []
    for s in range(BG_STAGES):
        active = s == stage
        pool = st.session_state.get(f"bg_urls{s}") or []
        # 负延迟按周期取模等于拨回 0 相位：新图组从第一张开始显示；
        # 取值只在真正换组时变，同组内 rerun 逐字节相同，动画不被打断
        delay = -BG_CYCLE * (int(st.session_state.get(f"bg_seq{s}", 0)) % 4)
        rules.append(
            f'.tv-bg-stage[data-stage="{s}"]{{opacity:{1 if active else 0};'
            f'transition-delay:{0 if active else BG_CROSS_LAG}s;}}'
        )
        for i in range(BG_SLIDES):
            img = f'background-image:url("{pool[i]}");' if i < len(pool) else ""
            rules.append(
                f'.tv-bg-stage[data-stage="{s}"] .tv-bg-s{i}'
                f'{{{img}animation-delay:{delay:.1f}s;}}'
            )
    # 行程结果页要长时间读表格文字，背景再压一档；空状态页保持原亮度展示风光
    dim, sat = ("0.55", "0.85") if mode == "result" else ("1", "1")
    rules.append(f".tv-bg{{--bg-dim:{dim};--bg-sat:{sat};}}")
    return (
        f'<div class="tv-bg-tag" data-scope="{escape(scope, quote=True)}" '
        f'data-stage="{stage}" data-mode="{mode}"></div>'
        f'<style>{"".join(rules)}</style>'
    )





# ---------------------------------------------------------------------------
# 全局 CSS（任务 5）：深灰蓝底 + 毛玻璃 + 渐变按钮 + 时间轴
# ---------------------------------------------------------------------------
GLOBAL_CSS = """
<style>
/* 0. 明确声明深色方案：页面本身就是深色，声明后 Chromium 的 ForceDark
   （--enable-features=WebContentsForceDark，第三方壳常见）会判定页面已适配，
   跳过对半透明层 / backdrop-filter 的反色注入，避免出现叠影。 */
:root {
    color-scheme: dark;
}

/* 1. 全局背景：交给下方「维度一」的主题背景统一接管，这里只定文字色 */
.stApp {
    color: #E2E8F0;
}

/* 2. 侧边栏配色见文件末尾的「磨砂玻璃」一节，这里只保证文字颜色 */
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
    /* stHeader 高 60px 且透明悬浮在内容之上，工具栏那一排还会再占十几像素：
       72px 起在矮窗口里刚好把「旅游规划助手」顶上沿咬掉。抬到 96px 起，
       主滚动容器的首屏留白就稳定大于悬浮头的下沿。 */
    padding-top: clamp(96px, 9vh, 120px);
    padding-bottom: 3rem;
    /* 原本写死 1320px：收起侧栏腾出的宽度被封顶吃掉，全变成居中的左右留白 */
    max-width: min(100% - clamp(24px, 4vw, 72px), 1760px);
}
/* 卡片墙不跟着一起变宽：正文段落保持可读行长，只有网格铺满 */
div[data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div[data-testid="stText"],
div[data-testid="stMainBlockContainer"] > div[data-testid="stVerticalBlock"] > div[data-testid="stMarkdownContainer"] {
    max-width: 92ch;
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
   照片贴顶、整张照片就是一颗透明按钮（点开详情），底部一条「套用这个行程」。
   卡片容器在 1.66/1.67 都是「直接装着 3 个 stElementContainer 的 stVerticalBlock」，
   所以只认这个结构，不认版本各异的边框 wrapper testid。
   ========================================================================== */
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] > div .pc-face) {
    position: relative;
    gap: 0 !important;
    padding: 0 !important;
    background: #0B1220 !important;
    border: none !important;
    border-radius: 16px !important;
    overflow: hidden !important;
    box-shadow: 0 8px 24px rgba(2, 6, 23, 0.45);
    transition: transform 0.3s ease, box-shadow 0.3s ease;
}
div[data-testid="stVerticalBlock"]:has(> div[data-testid="stElementContainer"] > div .pc-face):hover {
    transform: translateY(-5px);
    box-shadow: 0 22px 44px rgba(2, 6, 23, 0.68);
}

/* 照片那一格：高度统一由 --pc-face-h 控制（见下方 :root），
   保证 2 行 × 4 列的 8 张卡在 1440×900 下整卡落在输入卡上方。 */
div[data-testid="stElementContainer"]:has(.pc-face) {
    position: relative;
    height: var(--pc-face-h, 160px);
}
@keyframes cardRiseIn {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
}
.pc-face {
    position: absolute;
    inset: 0;
    overflow: hidden;
    pointer-events: none;
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    animation: cardRiseIn 0.4s ease-out both;
    will-change: transform, opacity;
}
.pc-shade {
    position: absolute;
    inset: 0;
    background: linear-gradient(to top,
                rgba(0, 0, 0, 0.90) 0%,
                rgba(0, 0, 0, 0.45) 38%,
                rgba(0, 0, 0, 0.00) 72%);
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
    font-size: clamp(13.5px, 1vw, 17px);
    font-weight: 800;
    line-height: 1.35;
    color: #FFFFFF;
    text-shadow: 0 2px 10px rgba(0, 0, 0, 0.60);
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
}
.pc-desc {
    margin-top: 5px;
    font-size: clamp(11px, .78vw, 12.5px);
    font-weight: 400;
    line-height: 1.55;
    color: #D3DCE8;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.55);
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
}
/* 天数与价格：挑行程最关心的两个数，直接摆在标题下方 */
.pc-meta {
    margin-top: 7px;
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    font-size: 12px;
    font-weight: 700;
    line-height: 1.5;
    color: #AEC1DA;
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.72);
}
.pc-price {
    padding: 1px 7px;
    font-size: 12.5px;
    font-weight: 800;
    color: #0B1220;
    background: linear-gradient(96deg, #FCD34D, #F59E0B);
    border-radius: 6px;
    text-shadow: none;
}
.pc-hint {
    position: absolute;
    top: 12px;
    right: 12px;
    padding: 4px 10px;
    font-size: 11.5px;
    font-weight: 700;
    color: #DBEAFE;
    background: rgba(15, 23, 42, 0.62);
    border: 1px solid rgba(147, 197, 253, 0.34);
    border-radius: 999px;
    opacity: 0;
    transform: translateY(-4px);
    transition: opacity 0.2s ease, transform 0.2s ease;
}
div[data-testid="stVerticalBlock"]:has(.pc-face):hover .pc-hint {
    opacity: 1;
    transform: translateY(0);
}

/* 紧跟照片的那颗按钮抬到照片上，撑满、隐形，只留点击 */
div[data-testid="stElementContainer"]:has(.pc-face) + div[data-testid="stElementContainer"] {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    width: 100%;
    height: var(--pc-face-h, 160px);
    z-index: 3;
}
div[data-testid="stElementContainer"]:has(.pc-face) + div [data-testid="stButton"] {
    height: 100%;
}
div[data-testid="stElementContainer"]:has(.pc-face) + div [data-testid="stButton"] button {
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    font-size: 0 !important;
    color: transparent !important;
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    opacity: 0;
}

/* 底部操作条：主 CTA，必须一眼看出是可点的按钮 */
div[data-testid="stElementContainer"]:has(.pc-face) ~ div[data-testid="stElementContainer"]:last-child {
    padding: 12px 14px 14px;
}
div[data-testid="stElementContainer"]:has(.pc-face) ~ div[data-testid="stElementContainer"]:last-child
    [data-testid="stButton"] button {
    width: 100% !important;
    margin: 0 !important;
    padding: 0.66rem 1rem !important;
    font-size: 14px !important;
    font-weight: 800 !important;
    letter-spacing: 0.3px;
    color: #FFFFFF !important;
    background: linear-gradient(96deg, #3B82F6 0%, #6366F1 55%, #8B5CF6 100%) !important;
    border: none !important;
    border-radius: 10px !important;
    box-shadow: 0 6px 18px rgba(59, 130, 246, 0.38) !important;
    transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
}
div[data-testid="stElementContainer"]:has(.pc-face) ~ div[data-testid="stElementContainer"]:last-child
    [data-testid="stButton"] button:hover {
    transform: translateY(-2px);
    filter: brightness(1.08);
    box-shadow: 0 10px 26px rgba(99, 102, 241, 0.52) !important;
}

/* ---------- 页首的范围提示（随目的地联动，原来挂在 rec-head 上） ---------- */
.pc-page-scope {
    font-size: 15px;
    font-weight: 800;
    color: #E2E9F5;
    letter-spacing: 0.4px;
}
.pc-page-scope::before {
    content: "";
    display: inline-block;
    width: 3px;
    height: 14px;
    margin-right: 8px;
    border-radius: 2px;
    background: linear-gradient(180deg, #3B82F6, #8B5CF6);
    vertical-align: -1px;
}

/* ==========================================================================
   问答式侧栏 + 收起态回弹 + 可点卡片
   ========================================================================== */
/* 管家气泡：靠 backdrop-filter 糊住身后的风光，字才立得住 */
section[data-testid="stSidebar"] [data-testid="stChatMessage"] {
    background: rgba(9, 15, 28, 0.46) !important;
    border: 1px solid rgba(148, 163, 184, 0.16) !important;
    border-radius: 14px;
    padding: 10px 12px !important;
    margin-bottom: 8px;
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
}
section[data-testid="stSidebar"] [data-testid="stChatMessage"] p {
    font-size: 13px;
    line-height: 1.75;
    margin: 0 0 4px 0;
}
section[data-testid="stSidebar"] [data-testid="stChatMessage"] ul {
    margin: 2px 0 4px 0;
    padding-left: 18px;
}
section[data-testid="stSidebar"] [data-testid="stChatMessage"] li {
    font-size: 12.5px;
    line-height: 1.7;
    color: #CBD5E1;
}
section[data-testid="stSidebar"] [data-testid="stChatMessageContent"] {
    min-height: 0 !important;
}
/* 输入框贴着侧栏底部，占位文案在窄侧栏里要能折行显示 */
section[data-testid="stSidebar"] [data-testid="stChatInput"] {
    background: transparent !important;
}
section[data-testid="stSidebar"] [data-testid="stChatInput"] textarea {
    font-size: 13px;
    min-height: 46px !important;
}
/* 「精确设置」弹层：把原来三组控件收进一个入口 */
section[data-testid="stSidebar"] [data-testid="stPopoverBody"] {
    background: rgba(8, 13, 26, 0.94) !important;
    border: 1px solid rgba(148, 163, 184, 0.22) !important;
    border-radius: 14px;
    padding: 10px 12px 14px 12px !important;
    max-height: 72vh;
    overflow-y: auto;
}

/* 收起侧栏：Streamlit 只做 -300px 平移，被 min-width 撑住的 52px 会露出来，
   主区也白留一条空带；收起态把面板压回 0 宽，让内容等比例左移补上。 */
section[data-testid="stSidebar"][data-collapsed="true"] {
    min-width: 0 !important;
    width: 0 !important;
    border-right: none !important;
    box-shadow: none !important;
    backdrop-filter: none !important;
    -webkit-backdrop-filter: none !important;
    overflow: hidden !important;
}
section[data-testid="stSidebar"][data-collapsed="true"] [data-testid="stSidebarContent"],
section[data-testid="stSidebar"][data-collapsed="true"] [data-testid="stSidebarUserContent"] {
    visibility: hidden !important;
}
.stApp:has(section[data-testid="stSidebar"][data-collapsed="true"]) [data-testid="stMainBlockContainer"] {
    padding-left: clamp(1rem, 3vw, 3.5rem);
}
/* 收起后首屏整体上移：标题少留一段头顶空白（.hero 那条是 !important，这里也得跟上） */
.stApp:has(section[data-testid="stSidebar"][data-collapsed="true"]) .hero {
    padding-top: clamp(4px, 1.2vh, 16px) !important;
}

/* 首屏自适应：标题压到一行小字，把首屏高度让给卡片 */
.hero {
    padding: clamp(6px, 1.6vh, 20px) 12px clamp(6px, 1.2vh, 14px) 12px !important;
}
.hero-title {
    font-size: clamp(22px, 2.3vw, 34px) !important;
}
.hero-sub {
    font-size: clamp(11.5px, 0.95vw, 13.5px) !important;
    max-width: min(92%, 640px) !important;
    margin-top: 6px !important;
    line-height: 1.6 !important;
}
/* 只装 <style> 的 markdown 容器高度为 0，却照样占掉 16px 的 flex gap：
   首屏顶部原本压着 3 份这种全局样式，白吃掉 48px。整颗摘掉，可见内容不受影响。 */
div[data-testid="stElementContainer"]:has(> div[data-testid="stMarkdown"] div > style:only-child) {
    display: none;
}
/* 卡片封面的高度：一屏 8 张（2 行 × 4 列）必须整卡落在吸底输入卡上方。
   实测 1440×900：卡片区从 250 开始，每张卡除封面外还有 69px 操作条 + 14px 行距，
   空态输入卡上沿在 718。要让第二行「一键生成」按钮底边（= 388 + 2×封面高）
   停在 718 之上，封面高只能取到 158px 左右。写成 50vh - 292px 是让更高的窗口
   按同样比例长大，矮窗口保底 158px，不把卡片压成一条。 */
:root { --pc-face-h: clamp(158px, calc(50vh - 292px), 280px); }
/* 卡片网格：列数随主区宽度增长（1 → 2 → 3 → 4 → 6）。
   这里用断点而不是 auto-fit，是因为 12 张卡只有被 12 整除的列数才不会出现
   最后一排缺格的空洞：2/3/4/6 都能整除，5 列就会留下 3 个空格。 */
div[data-testid="stHorizontalBlock"]:has(.pc-face) {
    display: grid !important;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-auto-flow: row dense;
    gap: clamp(10px, 1vw, 16px);
}
div[data-testid="stHorizontalBlock"]:has(.pc-face) > div[data-testid="stColumn"] {
    flex: none !important;      /* 交还给 grid，别让 flex 再算一遍宽度 */
    min-width: 0 !important;    /* Streamlit 列自带 min-width，会阻止收缩 */
    width: auto !important;
}
@media (min-width: 700px) {
    div[data-testid="stHorizontalBlock"]:has(.pc-face) {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
}
@media (min-width: 1024px) {
    div[data-testid="stHorizontalBlock"]:has(.pc-face) {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }
}
/* 一页 8 张，列数只能取 8 的因数，否则末行缺格、吸附高度也和「一屏」对不上。
   原来 ≥1600 给 6 列（12 张刚好两行），翻页之后 6 列会剩 2 个空格，改成 4 列并限宽。 */
@media (min-width: 1600px) {
    div[data-testid="stHorizontalBlock"]:has(.pc-face) {
        grid-template-columns: repeat(4, minmax(0, 1fr));
        max-width: 1400px;
        margin-inline: auto;
    }
}
@media (max-width: 560px) {
    div[data-testid="stHorizontalBlock"]:has(.pc-face) {
        grid-template-columns: 1fr;
    }
}

/* 首页：纵向整页吸附。所有页同时在 DOM 里，父容器就是唯一滚动区：
   隐藏滚动条、100dvh 一页、scroll-snap-align:start。
   主区本身不再滚（overflow:hidden），避免出现内外两层滚动条。 */
.st-key-home_pages {
    height: 100dvh !important;
    max-height: 100dvh !important;
    flex: none !important;
    overflow-y: auto;
    overflow-x: hidden;
    scroll-snap-type: y mandatory;
    scrollbar-width: none;             /* Firefox */
    -ms-overflow-style: none;          /* 旧 Edge */
    gap: 0 !important;                 /* 页容器之间的默认 flex gap 会破坏吸附点 */
}
.st-key-home_pages::-webkit-scrollbar { display: none; }
/* Streamlit 会把每个子块包进 stLayoutWrapper：吸附点与 100dvh 必须落在这一层，
   否则内层块再高，外层的自动高度也会把整页撑开。 */
.st-key-home_pages > div[data-testid="stLayoutWrapper"],
.st-key-home_pages [class*="st-key-home_page_"] {
    height: 100dvh !important;
    max-height: 100dvh !important;
    scroll-snap-align: start;
}
.st-key-home_pages > div[data-testid="stLayoutWrapper"] {
    flex: none !important;
}
.st-key-home_pages [class*="st-key-home_page_"] {
    display: flex !important;
    flex-direction: column;
    justify-content: center;
    gap: clamp(8px, 1.2vh, 14px) !important;
    padding: 0 clamp(4px, 1vw, 14px);
}
/* 首页主区让位给吸附容器：去掉上下留白，main 不再自己滚 */
main[data-testid="stMain"]:has(.st-key-home_pages) {
    overflow: hidden !important;
}
div[data-testid="stMainBlockContainer"]:has(.st-key-home_pages) {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}
/* 一页 2×4：桌面固定 4 列，行高压缩到 100dvh 内能放下两行 + hero */
.st-key-home_pages div[data-testid="stHorizontalBlock"]:has(.pc-face) {
    grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
    max-width: 1400px;
    margin-inline: auto;
    width: 100%;
}
.st-key-home_pages { --pc-face-h: clamp(120px, calc((100dvh - 420px) / 2), 240px); }
/* 末页给吸底输入卡让出高度；输入卡只在末页滑入，其余页 translateY 藏到视口下 */
.st-key-home_pages > div[data-testid="stLayoutWrapper"]:last-child [class*="st-key-home_page_"],
.st-key-home_pages > div[data-testid="stElementContainer"]:last-child [class*="st-key-home_page_"],
.st-key-home_pages > [class*="st-key-home_page_"]:last-child {
    padding-bottom: calc(var(--ci-composer-h, 180px) + 12px);
}
/* 左上角页码：固定定位，随滚动由前端脚本改文字 */
.pc-page {
    position: fixed;
    left: clamp(14px, 2.4vw, 34px);
    top: clamp(12px, 2vh, 24px);
    z-index: 24;
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 0;
    font-size: 13px;
    font-weight: 700;
    color: #8FA6C6;
    pointer-events: none;
}
.pc-page-no { color: #E2ECFA; }
.pc-page-scope { font-size: 12.5px; font-weight: 600; color: #8FA6C6; }

/* 吸底输入卡：st.container(key="composer") 会带上官方保证的 st-key-composer 类名。
   卡里的 chat_input 用 CSS 钉底而不是放在主区顶层，是为了让标题、对话回看、
   精确设置、按钮和输入框落在同一张视觉卡里。 */
.st-key-composer {
    position: fixed;
    inset-inline: clamp(10px, 2.6vw, 40px);
    bottom: clamp(10px, 1.6vh, 18px);
    z-index: 30;
    max-width: 1180px;
    margin-inline: auto;
    padding: 11px clamp(12px, 1.5vw, 20px) 13px clamp(12px, 1.5vw, 20px);
    background: rgba(15, 23, 42, 0.65);
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 18px;
    box-shadow: 0 18px 44px rgba(2, 6, 23, 0.55);
    /* 输入卡只在最后一页升起：transform 滑入滑出，不改 height，
       否则每页的吸附高度会跟着变。默认藏在视口下 24px。 */
    transform: translateY(calc(100% + 24px));
    opacity: 0;
    visibility: hidden;
    transition: transform .42s cubic-bezier(.22, .61, .36, 1), opacity .3s ease, visibility .42s;
}
html.ci-last-page .st-key-composer {
    transform: translateY(0);
    opacity: 1;
    visibility: visible;
}
/* 输入卡里的控件自己还铺着一层实色（主题给的），会把毛玻璃糊回一块板：
   输入框与内层容器全部让路，只留一条细边标出可输入区域。 */
.st-key-composer [data-testid="stChatInput"],
.st-key-composer [data-testid="stChatInput"] > div,
.st-key-composer div[data-baseweb="textarea"],
.st-key-composer div[data-baseweb="base-input"],
.st-key-composer div[data-baseweb="input"] {
    background: transparent !important;
    box-shadow: none !important;
}
.st-key-composer [data-testid="stChatInput"] {
    border-color: rgba(255, 255, 255, 0.12) !important;
}
.st-key-composer [data-testid="stChatInput"] textarea {
    background: transparent !important;
    color: #F1F5F9 !important;
}
.st-key-composer [data-testid="stChatInput"] textarea::placeholder {
    color: rgba(226, 232, 240, 0.55) !important;
    -webkit-text-fill-color: rgba(226, 232, 240, 0.55) !important;
}
/* 「精确设置」弹层也是玻璃：Streamlit 主题把 popover body 的底色写死，
   这里必须 !important 才盖得住。只作用在弹层本体，不给主内容区铺底，
   否则整页会发灰。 */
div[data-baseweb="popover"] div[data-testid="stPopoverBody"],
div[data-testid="stPopoverBody"] {
    background: rgba(15, 23, 42, 0.65) !important;
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    border: 1px solid rgba(255, 255, 255, 0.1) !important;
    border-radius: 16px !important;
    box-shadow: 0 24px 60px rgba(2, 6, 23, 0.55) !important;
}
/* 弹层里的下拉 / 日期 / 数字控件：主题给的是浅色实底（stSelectbox 的 react-aria
   分组、stDateInputField 都是 #F4F6F8），压在玻璃面板上像贴了三块白胶带。
   统一改成深色半透明 + 细边，文字与图标提亮，和面板保持同一套材质。
   注意：1.66 的下拉不再带 data-baseweb="select"，控件本体是 [role="group"]，
   所以两套选择器都要留着（老版本走 baseweb 分支）。 */
div[data-testid="stPopoverBody"] {
    color: #CBD5E1 !important;
}
div[data-testid="stPopoverBody"] div[data-testid="stDateInputField"],
div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] div[role="group"],
div[data-testid="stPopoverBody"] div[data-testid="stTextInput"] div[role="group"],
div[data-testid="stPopoverBody"] div[data-testid="stTextArea"] div[role="group"],
div[data-testid="stPopoverBody"] div[data-baseweb="select"] > div,
div[data-testid="stPopoverBody"] div[data-baseweb="input"],
div[data-testid="stPopoverBody"] div[data-baseweb="base-input"],
div[data-testid="stPopoverBody"] div[data-testid="stNumberInputContainer"],
div[data-testid="stPopoverBody"] div[data-testid="stDateInput"] > div,
div[data-testid="stPopoverBody"] div[data-testid="stTextInput"] > div,
div[data-testid="stPopoverBody"] div[data-testid="stTextArea"] > div {
    background: rgba(9, 16, 28, 0.72) !important;
    border: 1px solid rgba(148, 163, 184, 0.28) !important;
    border-radius: 8px !important;
    box-shadow: none !important;
}
div[data-testid="stPopoverBody"] div[data-testid="stDateInputField"]:hover,
div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] div[role="group"]:hover,
div[data-testid="stPopoverBody"] div[data-baseweb="select"] > div:hover,
div[data-testid="stPopoverBody"] div[data-baseweb="input"]:hover {
    background: rgba(15, 26, 44, 0.82) !important;
    border-color: rgba(148, 190, 240, 0.42) !important;
}
div[data-testid="stPopoverBody"] div[data-testid="stDateInputField"]:focus-within,
div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] div[role="group"]:focus-within,
div[data-testid="stPopoverBody"] div[data-baseweb="select"] > div:focus-within,
div[data-testid="stPopoverBody"] div[data-baseweb="input"]:focus-within {
    border-color: rgba(59, 130, 246, 0.85) !important;
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.18) !important;
}
/* 弹层被 portal 到 body 下，拿不到 .stApp 的深色文字，输入框会继承 #1B1F24 */
div[data-testid="stPopoverBody"] input,
div[data-testid="stPopoverBody"] textarea {
    color: #E8EEF7 !important;
    -webkit-text-fill-color: #E8EEF7 !important;
    background: transparent !important;
    caret-color: #93C5FD;
}
/* 日期不是 input：react-aria 把年月日拆成 contenteditable 的 span[role=spinbutton]，
   继承的是 portal 外面的 #1B1F24，深底上完全看不见 */
div[data-testid="stPopoverBody"] [data-testid="stDateInputFieldsScroller"] span,
div[data-testid="stPopoverBody"] [role="spinbutton"] {
    color: #E8EEF7 !important;
    -webkit-text-fill-color: #E8EEF7 !important;
    opacity: 1 !important;
}
div[data-testid="stPopoverBody"] div[data-baseweb="select"] svg,
div[data-testid="stPopoverBody"] div[data-testid="stDateInput"] svg,
div[data-testid="stPopoverBody"] div[data-testid="stSelectbox"] svg {
    fill: #9FB2CC !important;
    color: #9FB2CC !important;
}
/* 展开后的下拉菜单是 react-aria 的虚拟列表，容器 stSelectboxVirtualDropdown
   默认是纯白 #FFF，字号又是深色；不压的话点开就是一块白板 */
div[data-testid="stSelectboxVirtualDropdown"] {
    background: #0E1726 !important;
    border: 1px solid rgba(148, 163, 184, 0.28) !important;
    border-radius: 10px !important;
    box-shadow: 0 20px 50px rgba(2, 6, 23, 0.6) !important;
}
div[data-testid="stSelectboxVirtualDropdown"] [role="listbox"],
div[data-testid="stSelectboxVirtualDropdown"] [role="option"] {
    background: transparent !important;
    color: #E2E8F0 !important;
}
div[data-testid="stSelectboxVirtualDropdown"] [role="option"]:hover,
div[data-testid="stSelectboxVirtualDropdown"] [role="option"][aria-selected="true"] {
    background: rgba(59, 130, 246, 0.22) !important;
}
/* 日期选择器展开的日历：stDateInputCalendar 也是纯白底 + 深色字 */
div[data-testid="stDateInputCalendar"] {
    background: #0E1726 !important;
    color: #E2E8F0 !important;
    border: 1px solid rgba(148, 163, 184, 0.28) !important;
    border-radius: 10px !important;
    box-shadow: 0 20px 50px rgba(2, 6, 23, 0.6) !important;
}
div[data-testid="stDateInputCalendar"] [role="grid"],
div[data-testid="stDateInputCalendar"] [role="gridcell"],
div[data-testid="stDateInputCalendar"] [role="button"],
div[data-testid="stDateInputCalendar"] button {
    background: transparent !important;
    color: #E2E8F0 !important;
}
div[data-testid="stDateInputCalendar"] [role="gridcell"]:hover,
div[data-testid="stDateInputCalendar"] [role="button"]:hover,
div[data-testid="stDateInputCalendar"] button:hover {
    background: rgba(59, 130, 246, 0.22) !important;
}
div[data-testid="stDateInputCalendar"] [aria-selected="true"] {
    background: rgba(59, 130, 246, 0.5) !important;
    color: #FFFFFF !important;
}
/* 弹层里“?”的说明气泡同样是白底，会突兀地亮一下 */
div[data-testid="stTooltipContent"] {
    background: #0E1726 !important;
    color: #E2E8F0 !important;
    border: 1px solid rgba(148, 163, 184, 0.3) !important;
    box-shadow: 0 16px 40px rgba(2, 6, 23, 0.6) !important;
}
/* 展开后的下拉菜单 / 日历也是 baseweb popover，默认跟着主题走浅色；单独压成深色 */
div[data-baseweb="popover"] ul[role="listbox"],
div[data-baseweb="popover"] div[role="listbox"],
div[data-baseweb="popover"] div[data-baseweb="calendar"],
div[data-baseweb="popover"] div[data-baseweb="calendar"] > div {
    background: #0E1726 !important;
    color: #E2E8F0 !important;
}
div[data-baseweb="popover"] ul[role="listbox"] li:hover,
div[data-baseweb="popover"] div[role="option"]:hover,
div[data-baseweb="popover"] div[data-baseweb="calendar"] div[role="button"]:hover {
    background: rgba(59, 130, 246, 0.22) !important;
}
/* 卡片上方再铺一层由透明到深色的渐变：滚动时内容是被「雾」收掉的，
   而不是被一块硬边板子截断。pointer-events 关掉，不影响点卡片。 */
.st-key-composer::before {
    content: "";
    position: absolute;
    left: -4vw;
    right: -4vw;
    bottom: 100%;
    height: clamp(64px, 10vh, 120px);
    background: linear-gradient(180deg, rgba(5, 9, 18, 0) 0%, rgba(5, 9, 18, 0.45) 55%, rgba(5, 9, 18, 0.78) 100%);
    pointer-events: none;
}
.cp-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 4px 12px;
    margin-bottom: 4px;
}
.cp-title { font-size: 15px; font-weight: 800; color: #FFFFFF; }
.cp-sub { font-size: 12.5px; color: #9FB2CC; }
/* 对话回看只在真有内容时出现，默认收起成一条可滚的小窗 */
.st-key-composer div[data-testid="stVerticalBlockBorderWrapper"] {
    border: none !important;
    background: rgba(15, 23, 42, 0.55);
    border-radius: 12px;
    margin-bottom: 8px;
}
/* 主区底部让出输入卡的高度，最后一排卡片不会被盖住。
   输入卡高度不是固定的：带聊天记录时（对话回看 172px）会涨到约 300px，
   写死 224px 会把长表格的最后一段压在卡片底下。--ci-composer-h 由
   _embed_layout_sync() 里的 ResizeObserver 实时写到 <html> 上，
   拿不到时退回 224px（空态实测 168px + 56px 余量）。
   详情页 / 卡片弹窗不再渲染 composer：用 :has 把规则限定在卡真存在时，
   否则 --ci-composer-h 的残留值会在页面底部留出 300px 以上的空洞。 */
div[data-testid="stMainBlockContainer"]:has(.st-key-composer) {
    padding-bottom: calc(var(--ci-composer-h, 224px) + 16px) !important;
}
@media (max-width: 560px) {
    div[data-testid="stMainBlockContainer"]:has(.st-key-composer) {
        padding-bottom: calc(var(--ci-composer-h, 264px) + 16px) !important;
    }
}

/* ---------- 规划进度卡：等待期按真实阶段反馈 ---------- */
/* 「开始规划」到片头之间最长要等 50 秒上下，没有反馈就像卡死。这张卡与输入卡
   同一套玻璃材质；进度条宽度只跟 trace 里真实完成的阶段数走，不按秒数推进，
   所以慢请求也不会跑满卡住；秒表是前端时钟，另有一句提示说明「这是估算」。 */
.ci-plan-card {
    position: relative;
    max-width: 760px;
    margin: clamp(2px, 1.2vh, 16px) auto 0;
    padding: 16px clamp(16px, 2vw, 24px) 14px;
    background: rgba(15, 23, 42, 0.65);
    backdrop-filter: blur(16px) saturate(160%);
    -webkit-backdrop-filter: blur(16px) saturate(160%);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 18px;
    box-shadow: 0 18px 44px rgba(2, 6, 23, 0.55);
    text-align: left;
    /* 卡片挂载后会被 trace 反复重绘：提前声明合成层，避免首帧毛玻璃
       背景还没合成时整张卡发白（Chromium 对新建 backdrop-filter 层的已知抖动） */
    will-change: backdrop-filter;
    transform: translateZ(0);
}
.ci-plan-top {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    margin-bottom: 10px;
}
.ci-plan-title {
    display: inline-flex;
    align-items: center;
    gap: 9px;
    font-size: 16px;
    font-weight: 800;
    color: #FFFFFF;
}
.ci-plan-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: #60A5FA;
    box-shadow: 0 0 0 4px rgba(96, 165, 250, 0.18);
    animation: ci-plan-pulse 1.6s ease-in-out infinite;
}
.ci-plan-clock {
    font-size: 12.5px;
    color: #9FB2CC;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
}
.ci-plan-clock b {
    color: #BFDBFE;
    font-weight: 700;
}
.ci-plan-track {
    position: relative;
    height: 6px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.16);
    overflow: hidden;
}
.ci-plan-fill {
    display: block;
    position: relative;
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, rgba(59, 130, 246, 0.85), rgba(56, 189, 248, 0.95));
    transition: width 0.45s ease;
    overflow: hidden;
}
.ci-plan-fill::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(100deg, transparent 25%, rgba(255, 255, 255, 0.45) 50%, transparent 75%);
    transform: translateX(-100%);
    animation: ci-plan-shimmer 1.8s ease-in-out infinite;
}
.ci-plan-note {
    margin-top: 8px;
    font-size: 11.5px;
    line-height: 1.6;
    color: #8FA3BD;
}
.ci-plan-steps {
    list-style: none;
    margin: 11px 0 0;
    padding: 0;
    display: grid;
    gap: 6px;
}
.ci-plan-step {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0;
    padding: 0;
    font-size: 13.5px;
    line-height: 1.5;
    color: #8FA3BD;
}
.ci-plan-mark {
    flex: none;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 1px solid rgba(148, 163, 184, 0.36);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 10.5px;
    color: transparent;
    transition: background 0.25s ease, border-color 0.25s ease, color 0.25s ease;
}
.ci-plan-step.is-done {
    color: #CBD5E1;
}
.ci-plan-step.is-done .ci-plan-mark {
    border-color: rgba(52, 211, 153, 0.6);
    background: rgba(16, 185, 129, 0.18);
    color: #6EE7B7;
}
.ci-plan-step.is-live {
    color: #FFFFFF;
    font-weight: 650;
}
.ci-plan-step.is-live .ci-plan-mark {
    border-color: rgba(96, 165, 250, 0.8);
    background: rgba(59, 130, 246, 0.22);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.14);
    animation: ci-plan-pulse 1.4s ease-in-out infinite;
}
.ci-plan-sub {
    font-size: 11.5px;
    font-weight: 400;
    color: #7F93AE;
}
.ci-plan-live {
    margin-top: 10px;
    padding: 8px 10px;
    border-radius: 10px;
    background: rgba(9, 16, 28, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.18);
    font-size: 12.5px;
    line-height: 1.65;
    color: #B6C2D4;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.ci-plan-tag {
    display: inline-block;
    margin-right: 8px;
    padding: 1px 7px;
    border-radius: 999px;
    background: rgba(59, 130, 246, 0.18);
    color: #93C5FD;
    font-size: 11px;
}
.ci-plan-est {
    color: #6B7F98;
}
@keyframes ci-plan-shimmer {
    0% { transform: translateX(-100%); }
    60%, 100% { transform: translateX(100%); }
}
@keyframes ci-plan-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
@media (prefers-reduced-motion: reduce) {
    .ci-plan-dot,
    .ci-plan-fill::after,
    .ci-plan-step.is-live .ci-plan-mark { animation: none; }
}
/* 「取消规划」：视觉上是卡片右上角的 HTML 胶囊，真实 Streamlit 按钮被隐藏，
   只作为脚本点击的落点。之所以不直接给 st.button 绝对定位：st.container 的
   DOM 在各版本里定位上下文不一致，实测会跑到卡片左侧并竖排折行。 */
.st-key-ci_plan_wrap {
    position: relative;
    max-width: 760px;
    margin-inline: auto;
}
.st-key-ci_plan_wrap .ci-plan-card {
    max-width: none;
    margin-top: 0;
}
.st-key-ci_plan_wrap [data-testid="stButton"] {
    display: none;
}
.ci-plan-cancel {
    position: absolute;
    top: 12px;
    right: 14px;
    z-index: 6;
    min-height: 28px;
    padding: 0 12px;
    font: inherit;
    font-size: 12.5px;
    line-height: 26px;
    color: #CBD5E1;
    background: rgba(15, 23, 42, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.32);
    border-radius: 999px;
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.15s ease, border-color 0.15s ease, background 0.15s ease;
}
.ci-plan-cancel:hover {
    color: #FFFFFF;
    border-color: rgba(248, 113, 113, 0.6);
    background: rgba(127, 29, 29, 0.55);
}
.ci-plan-cancel:focus-visible {
    outline: none;
    box-shadow: 0 0 0 2px rgba(248, 113, 113, 0.35);
}
/* 已经点下去等 rerun 的那零点几秒：给一句明确的「正在退出」，别让人觉得点空了 */
.ci-plan-card.is-cancelled .ci-plan-cancel {
    color: #FCA5A5;
    border-color: rgba(248, 113, 113, 0.55);
    cursor: default;
}
/* 右上角的取消按钮让出位置，秒表不会被压住 */
.ci-plan-top {
    padding-right: 104px;
}
/* 结果详情页左上角的返回：紧凑胶囊，不用整行宽按钮 */
.st-key-result_home [data-testid="stButton"] > button {
    width: auto;
    min-height: 32px;
    padding: 0 14px;
    font-size: 13px;
    color: #CBD5E1;
    background: rgba(15, 23, 42, 0.6);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
}
.st-key-result_home [data-testid="stButton"] > button:hover {
    color: #FFFFFF;
    border-color: rgba(148, 190, 240, 0.55);
}

/* 详情弹窗：图集两列、视频 16:9，宽屏时并排 */
[data-testid="stDialog"] .dt-title {
    font-size: 22px;
    font-weight: 800;
    color: #FFFFFF;
    margin-bottom: 4px;
}
[data-testid="stDialog"] .dt-desc {
    font-size: 13.5px;
    color: #B6C2D4;
    line-height: 1.75;
}
[data-testid="stDialog"] .dt-sec {
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 1px;
    color: #93C5FD;
    margin: 16px 0 8px 0;
}
[data-testid="stDialog"] .dt-facts {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}
[data-testid="stDialog"] .dt-fact {
    padding: 5px 11px;
    font-size: 12.5px;
    color: #E2E8F0;
    background: rgba(51, 65, 85, 0.72);
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 999px;
}
[data-testid="stDialog"] .dt-video {
    position: relative;
    width: 100%;
    max-width: 820px;
    aspect-ratio: 16 / 9;
    border-radius: 14px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.22);
    background: #0B1220;
}
[data-testid="stDialog"] .dt-video iframe {
    position: absolute;
    inset: 0;
    width: 100% !important;
    height: 100% !important;
    border: none;
}
[data-testid="stDialog"] .dt-gal {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
}
[data-testid="stDialog"] .dt-gal img {
    width: 100%;
    height: 100%;
    min-height: 104px;
    object-fit: cover;
    border-radius: 12px;
    border: 1px solid rgba(148, 163, 184, 0.18);
}
/* 弹窗里左右两栏要能摊开，但宽度只能加在卡片上：
   [data-testid="stDialog"] 是铺满视口的遮罩 + flex 居中容器，
   给它设 max-width 会让遮罩只剩左边一条，卡片也跟着贴左、右侧不再压暗。 */
div[data-testid="stDialog"] > div {
    width: min(1280px, 94vw) !important;
    max-width: min(1280px, 94vw) !important;
    max-height: calc(100vh - 96px) !important;
    overflow-y: auto !important;
    /* 深色毛玻璃：主题是浅色，弹窗默认白底，这里必须整体压成深色，
       否则标题/正文改浅色后会变成白底白字。 */
    background: rgba(15, 23, 42, 0.82) !important;
    backdrop-filter: blur(22px) saturate(150%);
    -webkit-backdrop-filter: blur(22px) saturate(150%);
    border: 1px solid rgba(255, 255, 255, 0.12) !important;
    border-radius: 20px !important;
    box-shadow: 0 30px 80px rgba(2, 6, 23, 0.6) !important;
    color: #E2E8F0 !important;
}
/* 弹窗内所有 Streamlit 原生文字（caption / label / selectbox）统一提亮 */
[data-testid="stDialog"],
[data-testid="stDialog"] p,
[data-testid="stDialog"] label,
[data-testid="stDialog"] span {
    color: #CBD5E1;
}
[data-testid="stDialog"] .dt-hero {
    position: relative;
    width: 100%;
    height: clamp(150px, 22vh, 240px);
    margin: 0 0 14px 0;
    border-radius: 14px;
    overflow: hidden;
    background: #0B1220;
}
[data-testid="stDialog"] .dt-hero img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
}
[data-testid="stDialog"] .dt-hero-shade {
    position: absolute;
    inset: 0;
    background: linear-gradient(180deg, rgba(2, 6, 23, 0.05) 0%, rgba(15, 23, 42, 0.35) 55%, rgba(15, 23, 42, 0.95) 100%);
    pointer-events: none;
}
/* 弹窗里的下拉 / 日期控件：浅色主题会给白底，压在深色玻璃上很突兀 */
[data-testid="stDialog"] div[data-testid="stSelectbox"] div[role="group"],
[data-testid="stDialog"] div[data-testid="stDateInputField"],
[data-testid="stDialog"] div[data-baseweb="input"],
[data-testid="stDialog"] div[data-baseweb="select"] > div {
    background: rgba(9, 16, 28, 0.72) !important;
    border: 1px solid rgba(148, 163, 184, 0.28) !important;
    color: #E2E8F0 !important;
}
[data-testid="stDialog"] input {
    color: #E8EEF7 !important;
    -webkit-text-fill-color: #E8EEF7 !important;
}
/* 「只套用」= 深色幽灵按钮；「就按这个行程规划」仍走全局 primary 蓝紫渐变 */
[data-testid="stDialog"] [class*="st-key-fill_"] button {
    background: rgba(15, 23, 42, 0.55) !important;
    border: 1px solid rgba(148, 163, 184, 0.34) !important;
    color: #CBD5E1 !important;
    box-shadow: none !important;
}
[data-testid="stDialog"] [class*="st-key-fill_"] button:hover {
    background: rgba(51, 65, 85, 0.55) !important;
    border-color: rgba(148, 190, 240, 0.55) !important;
    color: #FFFFFF !important;
    transform: none;
    box-shadow: none;
}
/* 左栏（图集 + 视频 + 时间轴）比右栏长很多，右栏跟着滚就不留大片黑 */
[data-testid="stDialog"] div[data-testid="stColumn"]:has(.dt-panel) {
    position: sticky;
    top: 0;
    align-self: flex-start;
}
[data-testid="stDialog"] .dt-gal {
    grid-template-columns: repeat(3, minmax(0, 1fr));
}
[data-testid="stDialog"] .dt-gal img:first-child {
    grid-column: span 3;
    aspect-ratio: 16 / 7;
    min-height: 0;
}
/* 右栏事实卡：一块一个小节，纵向填满，不再是一条光秃秃的列 */
[data-testid="stDialog"] .dt-panel {
    margin-bottom: 12px;
    padding: 12px 14px 14px 14px;
    border-radius: 14px;
    background: rgba(30, 41, 59, 0.88);
    border: 1px solid rgba(148, 163, 184, .22);
    box-shadow: 0 10px 26px rgba(2, 6, 23, .42);
}
[data-testid="stDialog"] .dt-panel .dt-sec {
    margin: 0 0 8px 0;
}
[data-testid="stDialog"] .dt-li {
    font-size: 12.5px;
    line-height: 1.75;
    color: #D7E2F1;
}
/* 「这趟适合谁」用左侧短竖线收拢，跟上面「主要看点」的圆点列表拉开差别，
   右栏两块面板不至于看着像同一种东西。 */
[data-testid="stDialog"] .dt-fit {
    position: relative;
    padding-left: 12px;
    font-size: 13.5px;
    line-height: 1.6;
    color: #D7E2F1;
}
[data-testid="stDialog"] .dt-fit + .dt-fit {
    margin-top: 7px;
}
[data-testid="stDialog"] .dt-fit::before {
    content: "";
    position: absolute;
    left: 0;
    top: .42em;
    bottom: .42em;
    width: 2px;
    border-radius: 2px;
    background: rgba(96, 165, 250, .45);
}
/* 引用行故意做成另一种质地：一眼能看出是用户自己说过的话，不是系统的判断 */
[data-testid="stDialog"] .dt-fit-quote {
    margin-top: 10px;
    font-style: italic;
    color: #9FB2CC;
}
[data-testid="stDialog"] .dt-fit-quote::before {
    background: rgba(148, 163, 184, .28);
}
/* 启程片头：整屏覆盖，时间轴全在 CSS 里（st.markdown 会把 <script> 过滤掉，
   所以「描线 -> 推镜 -> 封面淡入 -> 收场」只能靠 animation-delay 串起来）。
   0.0s 地图淡入 / 0.55s 红线描出 / 2.05s 流光跑两趟 / 2.45s 推镜放大目的地
   / 3.85s 封面淡入 / 5.05s 整块退场。 */
[data-testid="stDialog"] .dt-intro {
    position: fixed;
    inset: 0;
    z-index: 1200;
    overflow: hidden;
    background: radial-gradient(120% 90% at 50% 32%, #0C1E36 0%, #050B16 72%);
    animation: dtIntroOut .65s ease 5.05s forwards;
}
[data-testid="stDialog"] .dt-intro-map {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    transform-origin: var(--dt-zoom-origin, 50% 50%);
    animation: dtIntroZoom 1.5s cubic-bezier(.66, .02, .34, 1) 2.45s forwards;
}
[data-testid="stDialog"] .dt-intro-svg {
    width: min(92vw, 1160px);
    height: auto;
    opacity: 0;
    animation: dtIntroIn .7s ease .05s forwards;
}
[data-testid="stDialog"] .dt-intro-svg .dt-grat line {
    stroke: rgba(120, 160, 210, .15);
    stroke-width: 1;
}
[data-testid="stDialog"] .dt-intro-svg .dt-dot {
    fill: rgba(120, 160, 210, .40);
}
[data-testid="stDialog"] .dt-intro-svg .dt-route {
    fill: none;
    stroke: #FF4D4D;
    stroke-width: 4;
    stroke-linecap: round;
    stroke-dasharray: 1;
    stroke-dashoffset: 1;
    filter: drop-shadow(0 0 7px rgba(255, 77, 77, .55));
    animation: dtRouteDraw 1.75s ease-out .55s forwards;
}
/* 描完再跑一趟流光：dasharray 用 pathLength=1 的相对单位，任何长度都是同一套数 */
[data-testid="stDialog"] .dt-intro-svg .dt-route-flow {
    fill: none;
    stroke: #FFE08A;
    stroke-width: 3;
    stroke-linecap: round;
    stroke-dasharray: .035 .965;
    opacity: 0;
    animation: dtRouteFlow 1.05s linear 2.05s 2 forwards;
}
[data-testid="stDialog"] .dt-intro-svg .dt-src,
[data-testid="stDialog"] .dt-intro-svg .dt-dst {
    transform-box: fill-box;
    transform-origin: center;
}
[data-testid="stDialog"] .dt-intro-svg .dt-src {
    fill: rgba(96, 165, 250, .9);
    animation: dtPing 1.15s ease-out .2s 2 both;
}
[data-testid="stDialog"] .dt-intro-svg .dt-dst {
    fill: #FF6B6B;
    animation: dtPing 1.15s ease-out 2.15s 2 both;
}
[data-testid="stDialog"] .dt-intro-svg text {
    font-size: 26px;
    fill: #C6D6EC;
    text-anchor: middle;
}
[data-testid="stDialog"] .dt-intro-svg .dt-dst-label {
    fill: #FFD9D9;
    font-weight: 700;
}
/* 经纬度标注：有它才读得出这是张有坐标的图，而不是一堆散点 */
[data-testid="stDialog"] .dt-intro-svg .dt-deg {
    font-size: 15px;
    fill: rgba(150, 180, 215, .38);
    text-anchor: start;
}
[data-testid="stDialog"] .dt-intro-hero {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    opacity: 0;
    animation: dtHeroIn 1.15s ease 3.85s forwards;
}
[data-testid="stDialog"] .dt-intro-cap {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 12%;
    text-align: center;
    font-size: clamp(20px, 3.1vw, 34px);
    letter-spacing: .04em;
    color: #EAF2FF;
    text-shadow: 0 2px 18px rgba(0, 0, 0, .7);
    opacity: 0;
    animation: dtHeroIn .9s ease 4.15s forwards;
}
/* 跳过按钮要浮在片头之上：z-index 比 .dt-intro 高就点得到，点一下重跑一轮，片头不再渲染。
   满宽按钮样式得按掉，否则这颗胶囊会横贯整个视口。 */
/* 右下角让到 64px：Streamlit Cloud 外壳的「Manage app」按钮占掉底部 44px，
   owner 登录时正好压住跳过按钮（实测重叠 101×22），普通访客看不到那个按钮。 */
.st-key-intro_skip {
    position: fixed;
    right: 22px;
    bottom: 64px;
    left: auto;
    width: max-content;
    z-index: 1500;
}
.st-key-intro_skip button {
    width: max-content;
    padding: 8px 18px;
    border-radius: 999px;
    background: rgba(9, 20, 36, .72);
    border: 1px solid rgba(148, 190, 240, .45);
    color: #DCE9FA;
    backdrop-filter: blur(6px);
}
@keyframes dtIntroIn { to { opacity: 1 } }
@keyframes dtIntroOut { to { opacity: 0; visibility: hidden; pointer-events: none } }
@keyframes dtRouteDraw { to { stroke-dashoffset: 0 } }
@keyframes dtRouteFlow {
    0% { opacity: .95; stroke-dashoffset: 1 }
    100% { opacity: .95; stroke-dashoffset: 0 }
}
@keyframes dtIntroZoom { to { transform: scale(2.35) } }
@keyframes dtHeroIn {
    from { opacity: 0; transform: translateY(10px) scale(1.04) }
    to { opacity: 1; transform: none }
}
@keyframes dtPing {
    0% { transform: scale(1); opacity: .95 }
    60% { transform: scale(1.9); opacity: .3 }
    100% { transform: scale(1); opacity: .95 }
}
/* 逐日骨架：一条竖线串起 D1/D2/D3，比一堆段落更像行程 */
[data-testid="stDialog"] .dt-tl {
    border-left: 2px solid rgba(96, 165, 250, .35);
    padding-left: 14px;
    display: grid;
    gap: 8px;
}
[data-testid="stDialog"] .dt-day {
    position: relative;
    font-size: 13px;
    color: #D7E2F1;
    line-height: 1.6;
}
[data-testid="stDialog"] .dt-day::before {
    content: "";
    position: absolute;
    left: -20px;
    top: 6px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #60A5FA;
    box-shadow: 0 0 0 3px rgba(96, 165, 250, .18);
}
[data-testid="stDialog"] .dt-dayno {
    display: inline-block;
    min-width: 30px;
    margin-right: 6px;
    font-weight: 800;
    color: #FCD34D;
}
/* 相关推荐的缩略卡 */
.pc-rel {
    position: relative;
    height: 104px;
    border-radius: 12px;
    overflow: hidden;
    background-size: cover;
    background-position: center;
    border: 1px solid rgba(148, 163, 184, .18);
    animation: cardRiseIn .4s ease-out both;
}
.pc-rel-body {
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: 8px;
    display: flex;
    flex-direction: column;
    gap: 2px;
}
.pc-rel-title {
    font-size: 12.5px;
    font-weight: 800;
    color: #FFFFFF;
    text-shadow: 0 2px 8px rgba(0, 0, 0, .65);
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}
.pc-rel-meta {
    font-size: 11px;
    color: #C6D5E8;
    text-shadow: 0 1px 6px rgba(0, 0, 0, .7);
}
[data-testid="stDialog"] .dt-link {
    font-size: 12.5px;
    color: #93C5FD;
}

/* ==========================================================================
   全屏动态背景：图片轮播 + 组间交叉淡化 + 暗色遮罩
   骨架 .tv-bg / .tv-bg-stage / .tv-bg-sN 由 background_layers_html() 恒定注入，
   地址、透明度与图层相位由 background_style_html() 单独注入；
   每张图的淡化曲线与轮播时长随 BG_* 常量生成，见下方占位符。
   ========================================================================== */
.tv-bg {
    position: fixed;
    inset: 0;
    z-index: -1;                       /* 沉到所有内容之下、html 深色底之上 */
    overflow: hidden;
    pointer-events: none;
    /* 隔离外壳：背景层自成一块合成层，主区的任何重绘/重排都不会连带它，
       也不会把它的绘制结果混到主区那一层里（Tabbit 软件合成下尤其明显） */
    contain: layout paint;
    will-change: opacity;
    --bg-dim: 1;                       /* 结果页由样式块压到 0.55，读表格时不抢注意力 */
    --bg-sat: 1;
}
.tv-bg-stage {
    position: absolute;
    inset: 0;
    opacity: 0;                        /* 哪一组在上由样式块的 opacity 决定 */
}
/* 换图组 = 两组图层互换透明度，所以是渐变过渡而不是硬切；旧组多顶 0.7 秒再淡出，
   给新图留出下载时间，避免切换瞬间露出底色 */
.tv-bg-stage[data-stage="0"] { opacity: 1; }
.tv-bg-img {
    position: absolute;
    inset: 0;
    background-size: cover;
    background-position: center;
    background-repeat: no-repeat;
    filter: brightness(var(--bg-dim)) saturate(var(--bg-sat));
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
.hero-title, .hero-sub, .pc-page-scope {
    text-shadow: 0 1px 8px rgba(0, 0, 0, 0.85);
}
/* 行程约束面板：半透明毛玻璃，让背景的风光透出来。墨底压到 0.5 以下仍可读，
   靠的是 backdrop-filter 把身后的图组糊掉，而不是把面板涂实 */
section[data-testid="stSidebar"] {
    background: rgba(10, 16, 30, 0.38) !important;
    backdrop-filter: blur(18px) saturate(160%);
    -webkit-backdrop-filter: blur(18px) saturate(160%);
    border-right: 1px solid rgba(147, 197, 253, 0.16) !important;
    box-shadow: 14px 0 36px -14px rgba(2, 6, 23, 0.55) !important;
}
/* 玻璃底上小字要有一圈投影，遇到亮天空才不糊 */
section[data-testid="stSidebar"] .sec-title,
section[data-testid="stSidebar"] .field-label,
section[data-testid="stSidebar"] [data-testid="stWidgetLabel"] span,
section[data-testid="stSidebar"] p {
    text-shadow: 0 1px 6px rgba(0, 0, 0, 0.8);
}
/* 内层容器不能再铺一层实色，否则毛玻璃白做 */
section[data-testid="stSidebar"] > div,
section[data-testid="stSidebar"] [data-testid="stSidebarContent"],
section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"],
section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] {
    background: transparent !important;
}
/* 输入控件跟着改成磨砂：底色用半透明深蓝而不是透白，字在任何画面上都压得住 */
section[data-testid="stSidebar"] div[data-baseweb="input"],
section[data-testid="stSidebar"] div[data-baseweb="textarea"],
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"],
section[data-testid="stSidebar"] div[data-testid="stDateInput"] > div,
section[data-testid="stSidebar"] div[data-testid="stTextInput"] > div,
section[data-testid="stSidebar"] div[data-baseweb="select"] > div {
    background: rgba(9, 15, 28, 0.42) !important;
    border: 1px solid rgba(148, 163, 184, 0.2) !important;
}
section[data-testid="stSidebar"] div[data-baseweb="input"]:hover,
section[data-testid="stSidebar"] div[data-baseweb="textarea"]:hover,
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"]:hover,
section[data-testid="stSidebar"] div[data-testid="stDateInput"] > div:hover,
section[data-testid="stSidebar"] div[data-testid="stTextInput"] > div:hover,
section[data-testid="stSidebar"] div[data-baseweb="select"] > div:hover {
    background: rgba(9, 15, 28, 0.52) !important;
    border-color: rgba(148, 163, 184, 0.32) !important;
}
section[data-testid="stSidebar"] div[data-baseweb="input"]:focus-within,
section[data-testid="stSidebar"] div[data-baseweb="textarea"]:focus-within,
section[data-testid="stSidebar"] div[data-testid="stNumberInputContainer"]:focus-within,
section[data-testid="stSidebar"] div[data-testid="stDateInput"] > div:focus-within,
section[data-testid="stSidebar"] div[data-testid="stTextInput"] > div:focus-within,
section[data-testid="stSidebar"] div[data-baseweb="select"] > div:focus-within {
    background: rgba(9, 15, 28, 0.58) !important;
    border-color: rgba(59, 130, 246, 0.85) !important;
}
/* 用户偏好「减少动态效果」时，只保留当前图组的第一张，停掉轮播与推近 */
@media (prefers-reduced-motion: reduce) {
    .tv-bg-img { animation: none !important; opacity: 0; }
    .tv-bg-s0 { opacity: 1; }
}
__BG_LAYER_CSS__
</style>
"""

# 图层动画与关键帧只取决于 BG_* 常量，生成一次贴进全局 CSS，与图组内容无关
GLOBAL_CSS = GLOBAL_CSS.replace("__BG_LAYER_CSS__", _bg_layer_css())


def inject_css() -> None:
    """每次 rerun 重发同一份字节的全局样式与背景骨架。

    这里不能按 session_state 哨兵「字面只注入一次」：Streamlit 每轮都会重算该
    markdown 节点，跳过不发的那一轮它会被当成过期节点回收，整页立刻变成无样式
    （实测：带哨兵的 #probe 第二次 rerun 后消失，恒定重发的 #always 一直在）。
    真正要保证的是「内容恒定」——字节相同、位置相同，主区不产生结构性变化；
    规划期 0.6s 的刷新已经在 fragment 里，这一块压根不会被重发。
    """
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
    # 骨架与样式分两段注入：骨架字节恒定，rerun 不重发、动画不被打断；
    # 样式块只随图组变化，组与组之间靠 opacity 过渡完成渐变
    st.markdown(background_layers_html(), unsafe_allow_html=True)
    st.markdown(background_style_html(), unsafe_allow_html=True)


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
    wanted_prefs = [o for o in (_preference_option(p) for p in (prefs or [])) if o]
    st.session_state["f_prefs"] = wanted_prefs
    st.session_state["f_pref_note"] = str(note or "")
    # checkbox_group 只认 f_prefs_i 这些逐选项 key，光写聚合键不会真的勾上
    for index, option in enumerate(PREFERENCE_OPTIONS):
        st.session_state[f"f_prefs_{index}"] = option in wanted_prefs

    wanted_company = [
        opt for opt in (_companion_option(tag) for tag in (payload.get("companions") or [])) if opt
    ]
    st.session_state["f_companions"] = wanted_company
    for index, option in enumerate(COMPANION_OPTIONS):
        st.session_state[f"f_companions_{index}"] = option in wanted_company
    st.session_state["f_request"] = payload.get("request", CONSTRAINT_DEFAULTS["request"])
    start = payload.get("start_date")
    span = max(0, int(payload.get("days") or CONSTRAINT_DEFAULTS["days"]) - 1)
    if not start:
        # 卡片没写日期就沿用当前出发日，但回程一定要按卡片天数重算：
        # 天数滑块早已改成「出发 + 回程」，不动回程等于卡片的 days 白填
        start = st.session_state.get("f_start") or (date.today() + timedelta(days=1))
    start_day = date.fromisoformat(str(start)) if isinstance(start, str) else start
    st.session_state["f_start"] = start_day
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


def _scroll_top_html() -> str:
    """翻页后把主滚动容器拉回页首。

    st.markdown 会吃掉 <script>，只能走 components.v1.html 的 iframe；
    Streamlit 里真正在滚的是 main[data-testid="stMain"]，父页面同源可以直接操作。
    重排要一两帧，补两次延时兜底；跨域被拦时静默失败，不影响翻页本身。
    """
    return """
<script>
(function () {
  var p = window.parent;
  function jump() {
    try {
      var main = p.document.querySelector('main[data-testid="stMain"]');
      if (main) { main.scrollTo({ top: 0, behavior: 'smooth' }); }
      p.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (e) {}
  }
  jump();
  requestAnimationFrame(jump);
  setTimeout(jump, 140);
  setTimeout(jump, 360);
})();
</script>
"""


def _embed_scroll_top() -> None:
    """把回顶脚本塞进页面：st.iframe 允许脚本执行且与主页面同源；
    老版本没有 st.iframe，退回 components.v1.html（同一份 HTML）。"""
    html = "<html><body style='margin:0'>" + _scroll_top_html() + "</body></html>"
    frame = getattr(st, "iframe", None)
    if callable(frame):
        try:
            frame(html, height=1)
            return
        except TypeError:
            pass
    st_components.html(html, height=0)


def _home_scroll_html(total: int) -> str:
    """首页整页吸附：滚轮一次切一页、页码实时更新、末页升起输入框、邻页懒加载封面。

    全部跑在父页面上下文（st.iframe 与主页面同源）：
    - wheel 必须 passive:false 才能 preventDefault；一次切换后冻结 800ms，
      避免传统鼠标一滚就飞过两三页；
    - IntersectionObserver 盯最后一页，用 <html> 上的 ci-last-page 类驱动
      composer 的 translateY 滑入滑出，不改高度；
    - 封面按当前页 ±1 懒加载，66 张卡片虽然都在 DOM 里，但不会同时下载 66 张图。
    """
    _ = total
    return """
<script>
(function () {
  var p = window.parent, doc;
  try { doc = p.document; } catch (e) { return; }
  var sc = doc.querySelector('.st-key-home_pages');
  if (!sc) return;
  var pages = Array.prototype.slice.call(
    sc.querySelectorAll('[class*="st-key-home_page_"]')
  );
  if (!pages.length) return;
  var last = pages.length - 1;
  var root = doc.documentElement;
  var idx = Math.max(0, Math.min(last, Math.round(sc.scrollTop / (sc.clientHeight || 1))));
  var locked = 0, raf = 0;
  // 上一轮如果停在末页，<html> 上的类会留到新页面；先清掉，等 IO 重新判定
  root.classList.remove('ci-last-page');

  function updateLabel() {
    var el = doc.querySelector('.pc-page');
    if (!el) return;
    var no = el.querySelector('.pc-page-no');
    if (no) no.textContent = '第 ' + (idx + 1) + ' / ' + pages.length + ' 页';
  }

  function loadNeighbours() {
    pages.forEach(function (pg, pi) {
      if (Math.abs(pi - idx) > 1) return;
      var faces = pg.querySelectorAll('.pc-face[data-bg]');
      Array.prototype.forEach.call(faces, function (face) {
        if (face.dataset.loaded) return;
        face.dataset.loaded = '1';
        face.style.backgroundImage = "url('" + face.dataset.bg + "')";
      });
    });
  }

  function onScroll() {
    if (raf) return;
    raf = p.requestAnimationFrame(function () {
      raf = 0;
      var h = sc.clientHeight || 1;
      var next = Math.max(0, Math.min(last, Math.round(sc.scrollTop / h)));
      if (next !== idx) { idx = next; updateLabel(); loadNeighbours(); }
    });
  }

  sc.addEventListener('scroll', onScroll, { passive: true });
  sc.addEventListener('wheel', function (e) {
    if (!e.deltaY) return;
    // 滚轮事件必须拦住默认滚动，否则浏览器会自己连续滚，snap 也拉不住
    e.preventDefault();
    var now = Date.now();
    if (now < locked) return;
    var target = Math.max(0, Math.min(last, idx + (e.deltaY > 0 ? 1 : -1)));
    if (target === idx) return;
    locked = now + 800;
    sc.scrollTo({ top: target * sc.clientHeight, behavior: 'smooth' });
  }, { passive: false });

  if (p.IntersectionObserver) {
    var io = new p.IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        root.classList.toggle('ci-last-page', entry.isIntersecting);
      });
    }, { root: sc, threshold: 0.35 });
    io.observe(pages[last]);
  }

  updateLabel();
  loadNeighbours();
})();
</script>
"""


def _embed_home_scroll(total: int) -> None:
    """挂首页吸附脚本（1px iframe，不占版面）。"""
    html = "<html><body style='margin:0'>" + _home_scroll_html(total) + "</body></html>"
    frame = getattr(st, "iframe", None)
    if callable(frame):
        try:
            frame(html, height=1)
            return
        except TypeError:
            pass
    st_components.html(html, height=0)



def _layout_sync_html() -> str:
    """把吸底输入卡的实时高度写到父页面 <html> 的 --ci-composer-h 上。

    主区 padding-bottom 靠这个变量跟着输入卡走：空态约 168px，带聊天记录时
    对话回看会长到约 300px。ResizeObserver 盯住高度变化，窗口缩放和
    聊天记录增删都能立刻反映到留白，不用等下一次 rerun。
    """
    return """
<script>
(function () {
  var p = window.parent;
  var doc, comp;
  try { doc = p.document; comp = doc.querySelector('.st-key-composer'); } catch (e) { return; }
  if (!comp) return;
  function apply() {
    var h = Math.round(comp.getBoundingClientRect().height);
    if (h > 0) doc.documentElement.style.setProperty('--ci-composer-h', h + 'px');
  }
  apply();
  try {
    if (p.ResizeObserver) new p.ResizeObserver(apply).observe(comp);
  } catch (e) {}
  [80, 260, 700, 1400].forEach(function (ms) { setTimeout(apply, ms); });
  p.addEventListener('resize', apply);
})();
</script>
"""


def _embed_layout_sync() -> None:
    """在每个渲染轮末尾挂一次高度同步脚本（1px iframe，不占版面）。"""
    html = "<html><body style='margin:0'>" + _layout_sync_html() + "</body></html>"
    frame = getattr(st, "iframe", None)
    if callable(frame):
        try:
            frame(html, height=1)
            return
        except TypeError:
            pass
    st_components.html(html, height=0)


def _plan_ticker_html(run_id: str) -> str:
    """进度卡的秒表与提示轮播。

    只做三件事：把 #ci-plan-elapsed 按前端时钟刷新、每 8 秒换一句提示、
    把卡片里那个 HTML 取消按钮的按下/点击转给被隐藏的真实 st.button。
    进度条宽度由服务端按真实阶段重绘，这里绝不碰——避免出现「匀速假进度」。
    每次规划带一个新的 run_id，父页面据此重置起点，不会沿用上一次的秒数；
    卡片被清掉后定时器自己停，不会在后面的页面上空转。
    """
    return f"""
<script>
(function () {{
  var p = window.parent, doc;
  try {{ doc = p.document; }} catch (e) {{ return; }}
  var RUN = "{run_id}";
  var HINTS = [
    "这一步通常最花时间，请稍候…",
    "正在核对真实的地图与天气数据，不会凭空编景点。",
    "接口偶尔会慢一点；进度只按已完成的阶段走，不会停在假的位置。"
  ];
  if (p.__ciPlanRun !== RUN) {{
    p.__ciPlanRun = RUN;
    p.__ciPlanStart = Date.now();
    p.__ciPlanHint = 0;
  }}
  // 委托监听：每次 ticker 脚本执行都重绑一次，不能只绑一次。上一次规划的 iframe
  // 被销毁时，浏览器会把挂在父文档上的监听一并回收，但全局标志还留在父窗口上，
  // 新 iframe 若据此跳过绑定，取消按钮就会安静地失效（本地第二次规划实测）。
  // 用 pointerdown 而不是 click：卡片每 0.6s 整块重绘，按下到抬起之间若赶上一次
  // 重绘，节点被换掉后 click 会派发到共同祖先，绑 click 就会丢点击。
  if (p.__ciCancelUnbind) {{
    try {{ p.__ciCancelUnbind(); }} catch (e) {{}}
  }}
  var fire = function (hit) {{
    if (!hit || hit.getAttribute('data-ci-fired') === '1') return;
    hit.setAttribute('data-ci-fired', '1');
    var card = doc.querySelector('.ci-plan-card');
    if (card) card.classList.add('is-cancelled');
    hit.textContent = '正在退出…';
    hit.disabled = true;
    var btn = doc.querySelector('.st-key-ci_plan_wrap [data-testid="stButton"] button')
      || doc.querySelector('.st-key-ci_plan_wrap button');
    if (btn) btn.click();
  }};
  var onDown = function (ev) {{
    var node = ev.target;
    if (node && node.closest) fire(node.closest('[data-ci-cancel]'));
  }};
  var onHit = function (ev) {{
    var node = ev.target;
    if (!node || !node.closest) return;
    var hit = node.closest('[data-ci-cancel]');
    if (!hit) return;
    ev.preventDefault();
    fire(hit);
  }};
  doc.addEventListener('pointerdown', onDown, true);
  doc.addEventListener('click', onHit, true);
  p.__ciCancelUnbind = function () {{
    doc.removeEventListener('pointerdown', onDown, true);
    doc.removeEventListener('click', onHit, true);
  }};
  function alive() {{ return !!doc.querySelector('.ci-plan-card'); }}
  // 背景层去重：首页和规划页在脚本里位置不同，Streamlit 不会回收首页那份
  // .tv-bg，于是 DOM 里同时留着两层全屏背景（各自跑自己的图片轮播）。两层
  // 相位不同，叠在一起就是背景不停闪。每 tick 只保留最上层（本轮渲染的这份）。
  function dedupeBg() {{
    var list = doc.querySelectorAll('.tv-bg');
    for (var i = 0; i < list.length - 1; i++) list[i].remove();
  }}
  function paint() {{
    dedupeBg();
    if (!alive()) {{
      clearInterval(p.__ciPlanTimer);
      p.__ciPlanTimer = null;
      return;
    }}
    var node = doc.getElementById('ci-plan-elapsed');
    if (node) node.textContent = ((Date.now() - p.__ciPlanStart) / 1000).toFixed(1);
  }}
  function rotate() {{
    if (!alive()) {{
      clearInterval(p.__ciPlanRotate);
      p.__ciPlanRotate = null;
      return;
    }}
    var node = doc.getElementById('ci-plan-hint');
    if (!node) return;
    p.__ciPlanHint = ((p.__ciPlanHint || 0) + 1) % HINTS.length;
    node.textContent = HINTS[p.__ciPlanHint];
  }}
  paint();
  if (p.__ciPlanTimer) clearInterval(p.__ciPlanTimer);
  p.__ciPlanTimer = setInterval(paint, 100);
  if (p.__ciPlanRotate) clearInterval(p.__ciPlanRotate);
  p.__ciPlanRotate = setInterval(rotate, 8000);
}})();
</script>
"""


def _embed_plan_ticker(run_id: str | None = None) -> None:
    """把秒表脚本塞进 1px iframe（st.iframe 允许同源脚本；老版本退回 components）。

    run_id 由 _PlanJob 固定：后台任务期间主脚本每 0.8s 轮询重绘一次卡片，
    若每轮都换 id，前端秒表会被反复清零。
    """
    if run_id is None:
        run_id = str(time.time_ns())
    html = "<html><body style='margin:0'>" + _plan_ticker_html(run_id) + "</body></html>"
    frame = getattr(st, "iframe", None)
    if callable(frame):
        try:
            frame(html, height=1)
            return
        except TypeError:
            pass
    st_components.html(html, height=0)


def render_empty_state() -> None:
    """首屏视觉中心：Agent 身份 + 纵向 scroll snap 的多页推荐卡片。

    一页 8 张（4 列 × 2 行），页数 = ceil(卡片总数 / 8)。所有页同时在 DOM 里，
    由前端 CSS scroll-snap 吸附、滚轮一次切一页；服务端不再维护页码。
    封面图只预加载第一页，其余页由滚动脚本按「邻页 ±1」懒加载。
    """
    cards, scope = pick_recommendations()
    total = max(1, math.ceil(len(cards) / PAGE_SLOTS))
    first_page = cards[:PAGE_SLOTS]

    # 只预加载第一页的封面：66 张全预载会白吃好几 MB 带宽。
    # 预载必须挂在比卡片 CSS 更早的块里（这里的 hero 哨兵不可见，只负责锚点）。
    st.markdown(
        "<div class='hero' style='display:none'><div class='hero-title'></div></div>"
        + _cover_preload_html(first_page),
        unsafe_allow_html=True,
    )
    # 预载哨兵之后、66 张卡之前注入全局样式：
    # 否则卡片会先按自动高度摊开（一屏看到多页），再被 100dvh 顶回去。
    inject_css()
    # 第一页的图走 CSS 保证首屏即出；其余页由脚本按 data-bg 懒加载
    st.markdown(_card_photo_css(first_page), unsafe_allow_html=True)
    # 页码浮在左上角，随滚动实时更新；不参与任何一页的排版
    st.markdown(
        f"<div class='pc-page'><span class='pc-page-no'>第 1 / {total} 页</span>"
        f"<span class='pc-page-scope'>{escape(scope)}</span></div>",
        unsafe_allow_html=True,
    )

    with st.container(key="home_pages"):
        for page in range(total):
            page_cards = cards[page * PAGE_SLOTS: (page + 1) * PAGE_SLOTS]
            with st.container(key=f"home_page_{page}"):
                if page == 0:
                    # hero 只属于第一页：跟着 scroll snap 一起滚走，不占后续页的高度
                    st.markdown(
                        "<div class='hero'>"
                        "<div class='hero-title'>✈️ 旅游规划助手</div>"
                        "<div class='hero-sub'>挑一张卡片直接出行程，或在最下方一句话告诉管家。</div>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                for row_start in (0, 4):
                    row_cards = page_cards[row_start: row_start + 4]
                    if not row_cards:
                        continue
                    cols = st.columns(4, gap="medium")
                    for slot, (col, preset) in enumerate(zip(cols, row_cards)):
                        # 全局序号：_card_photo_css 与 data-bg 都按这个序号对图
                        order = page * PAGE_SLOTS + row_start + slot
                        with col:
                            with card():
                                st.markdown(_card_face(preset, order, slot), unsafe_allow_html=True)
                                # 不加 help：Streamlit 会把按钮包进 tooltip 容器，照片上的点击层就不好铺满
                                if st.button("看详情", key=f"detail_{preset['key']}"):
                                    st.session_state["_open_detail"] = preset["key"]
                                    st.rerun()
                                if full_width_button("⚡ 一键生成这个行程", key=f"preset_{preset['key']}"):
                                    # 不再只回填侧栏：套用即开跑，用户不用再点一次「开始规划」
                                    st.session_state["_pending_preset"] = preset
                                    st.session_state["_run_preset"] = True
                                    st.rerun()

    _embed_home_scroll(total)



def _card_face(preset: dict[str, Any], slot: int, delay: int | None = None) -> str:
    """卡片正面浮层：遮罩、标签、标题、天数与价格。点击交给底下的照片按钮。

    slot 决定挂哪一条 background-image 规则（全局序号），delay 决定飘上来的时机
    （页内序号，翻页时每一页都从 0 开始错落）。
    """
    payload = preset.get("payload") or {}
    days = int(payload.get("days") or 0)
    people = int(payload.get("people") or 0)
    budget = float(payload.get("budget") or 0)
    bits: list[str] = []
    if days:
        bits.append(f"🗓 {days} 天 {max(days - 1, 0)} 晚")
    if people:
        bits.append(f"👥 {people} 人")
    meta = " · ".join(bits)
    if budget:
        per = budget / people if people else budget
        label = "人均" if people > 1 else "预算"
        meta += f" · {label} <span class='pc-price'>¥{per:,.0f}</span>"
    return (
        f"<div class='pc-face pc-face-{slot}' data-bg=\"{escape(_cover_url(preset['img']), quote=True)}\" "
        f"style='animation-delay:{(slot if delay is None else delay) * 60}ms'>"
        "<div class='pc-shade'></div>"
        f"<div class='pc-tag'>{escape(preset['tag'])}</div>"
        "<div class='pc-hint'>👁 看详情</div>"
        "<div class='pc-body'>"
        f"<div class='pc-title'>{escape(preset['title'])}</div>"
        f"<div class='pc-desc'>{escape(preset['desc'])}</div>"
        f"<div class='pc-meta'>{meta}</div>"
        "</div>"
        "</div>"
    )


def _related_face(preset: dict[str, Any], slot: int) -> str:
    """相关推荐的小卡：图走内联背景，省一份按槽位生成的样式。"""
    payload = preset.get("payload") or {}
    days = int(payload.get("days") or 0)
    people = int(payload.get("people") or 0)
    budget = float(payload.get("budget") or 0)
    price = f"　·　<span class='pc-price'>人均 ¥{budget / people:,.0f}</span>" if people and budget else ""
    return (
        f"<div class='pc-rel' style=\"animation-delay:{slot * 60}ms;"
        f"background-image:url('{preset['img']}')\">"
        "<div class='pc-shade'></div>"
        "<div class='pc-rel-body'>"
        f"<span class='pc-rel-title'>{escape(preset['title'])}</span>"
        f"<span class='pc-rel-meta'>🗓 {days} 天 {max(days - 1, 0)} 晚{price}</span>"
        "</div></div>"
    )


# ---------------------------------------------------------------------------
# 启程片头：出发地 -> 目的地的航线示意（纯 SVG + CSS，不引第三方地图）
# ---------------------------------------------------------------------------
# 这里有意不画国界线：公开版图要审图号，少一段岛礁就是硬伤，而且接高德静态地图要吃配额、
# 还得算投影对齐。经纬网 + 城市点位当「航线示意图」用，视觉上就是航线动画，
# 并且不依赖任何外部请求——离线套件能逐字断言画了什么。
_MAP_BOX = (73.0, 135.5, 16.0, 54.0)  # 经度 min/max、纬度 min/max
_MAP_W, _MAP_H = 1000, 742

CITY_XY: dict[str, tuple[float, float]] = {
    # 卡片覆盖的目的地
    "北京市": (116.41, 39.90),
    "上海市": (121.47, 31.23),
    "广州市": (113.26, 23.13),
    "深圳市": (114.06, 22.55),
    "成都市": (104.07, 30.57),
    "杭州市": (120.15, 30.27),
    "西安市": (108.94, 34.34),
    "厦门市": (118.09, 24.48),
    "大理市": (100.22, 25.62),
    "甘孜州": (101.96, 30.05),
    "梅州市": (116.12, 24.29),
    # 出发地候选（都是常见枢纽）
    "重庆市": (106.55, 29.56),
    "南京市": (118.80, 32.06),
    "武汉市": (114.30, 30.60),
    "长沙市": (112.94, 28.23),
    "郑州市": (113.63, 34.75),
    "天津市": (117.20, 39.13),
    "青岛市": (120.38, 36.07),
    "沈阳市": (123.43, 41.80),
    "哈尔滨市": (126.53, 45.80),
    "昆明市": (102.83, 24.88),
    "贵阳市": (106.63, 26.65),
    "兰州市": (103.83, 36.06),
    "乌鲁木齐市": (87.62, 43.79),
    "三亚市": (109.51, 18.25),
    "苏州市": (120.59, 31.30),
    "福州市": (119.30, 26.08),
    "济南市": (117.00, 36.65),
    "合肥市": (117.28, 31.86),
    "南昌市": (115.86, 28.68),
    "南宁市": (108.37, 22.82),
    "长春市": (125.32, 43.90),
    "大连市": (121.54, 38.95),
}

ORIGIN_CITIES = [
    "北京市", "上海市", "广州市", "深圳市", "杭州市", "成都市", "重庆市", "南京市",
    "武汉市", "西安市", "苏州市", "天津市", "青岛市", "济南市", "郑州市", "长沙市",
    "南昌市", "合肥市", "福州市", "厦门市", "昆明市", "贵阳市", "南宁市", "大连市",
    "乌鲁木齐市",
]
ORIGIN_DEFAULT = "北京市"


def _map_xy(lng: float, lat: float) -> tuple[float, float]:
    """经纬度 -> 示意图像素。经度按中纬度压缩，画出来才不会是竖长的一条。"""
    lng_min, lng_max, lat_min, lat_max = _MAP_BOX
    per_lat = _MAP_H / (lat_max - lat_min)
    per_lng = per_lat * math.cos(math.radians((lat_min + lat_max) / 2))
    return (lng - lng_min) * per_lng, (lat_max - lat) * per_lat


def _route_arc(x1: float, y1: float, x2: float, y2: float) -> str:
    """两点之间的航线弧：取法线方向的二次贝塞尔，控制点一律朝画面上方鼓。"""
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    dx, dy = x2 - x1, y2 - y1
    span = math.hypot(dx, dy) or 1.0
    bend = min(110.0, span * 0.24)
    cx, cy = mx - dy / span * bend, my + dx / span * bend
    if cy > my:
        cx, cy = mx + dy / span * bend, my - dx / span * bend
    return f"M {x1:.1f} {y1:.1f} Q {cx:.1f} {cy:.1f} {x2:.1f} {y2:.1f}"


def _graticule_svg() -> str:
    bits = []
    for lng in range(80, 136, 10):
        x = _map_xy(float(lng), 0.0)[0]
        bits.append(f"<line x1='{x:.1f}' y1='0' x2='{x:.1f}' y2='{_MAP_H}'/>")
        bits.append(f"<text class='dt-deg' x='{x + 6:.1f}' y='{_MAP_H - 10:.0f}'>{lng}°E</text>")
    for lat in range(20, 51, 10):
        y = _map_xy(0.0, float(lat))[1]
        bits.append(f"<line x1='0' y1='{y:.1f}' x2='{_MAP_W}' y2='{y:.1f}'/>")
        bits.append(f"<text class='dt-deg' x='8' y='{y - 8:.1f}'>{lat}°N</text>")
    return "".join(bits)


def _intro_html(preset: dict[str, Any], origin: str) -> str:
    """片头一整块覆盖弹窗：经纬网淡入 -> 红线描出航线 -> 推镜放大目的地 -> 封面淡入。

    没有目的地坐标就不播，宁可不演也不能把点画歪。
    """
    city = str((preset.get("payload") or {}).get("city") or "")
    dst = CITY_XY.get(city)
    if not dst:
        return ""
    dx_, dy_ = _map_xy(*dst)
    # 出发地和目的地同城时不画航线：两点重合，弧退化成看不见的一小截，字幕也会自问自答
    src = CITY_XY.get(origin) if origin != city else None

    others = "".join(
        f"<circle class='dt-dot' cx='{_map_xy(*xy)[0]:.1f}' cy='{_map_xy(*xy)[1]:.1f}' r='2.6'/>"
        for name, xy in CITY_XY.items()
        if name not in (city, origin)
    )
    route = ""
    if src:
        sx, sy = _map_xy(*src)
        d = _route_arc(sx, sy, dx_, dy_)
        route = (
            f"<path class='dt-route' pathLength='1' d='{d}'/>"
            f"<path class='dt-route-flow' pathLength='1' d='{d}'/>"
            f"<circle class='dt-src' cx='{sx:.1f}' cy='{sy:.1f}' r='6.5'/>"
            f"<text class='dt-src-label' x='{sx:.1f}' y='{sy - 16:.1f}'>{escape(_stem(origin))}</text>"
        )
    svg = (
        f"<svg class='dt-intro-svg' viewBox='0 0 {_MAP_W} {_MAP_H}' preserveAspectRatio='xMidYMid meet'>"
        f"<g class='dt-grat'>{_graticule_svg()}{others}</g>"
        f"{route}"
        f"<circle class='dt-dst' cx='{dx_:.1f}' cy='{dy_:.1f}' r='8.5'/>"
        f"<text class='dt-dst-label' x='{dx_:.1f}' y='{dy_ - 20:.1f}'>{escape(_stem(city))}</text>"
        f"</svg>"
    )
    img = str(preset.get("img") or "")
    hero = f"<img class='dt-intro-hero' src='{img}' alt=''>" if img else ""
    if src:
        cap = f"从{_stem(origin)}出发 → {_stem(city)}"
    elif origin == city:
        cap = f"{_stem(city)}就是出发地，落地直接开逛"
    else:
        cap = f"目的地：{_stem(city)}"
    zoom = f"{dx_ / _MAP_W * 100:.1f}% {dy_ / _MAP_H * 100:.1f}%"
    return (
        f"<div class='dt-intro' style='--dt-zoom-origin:{zoom}'>"
        f"<div class='dt-intro-map'>{svg}</div>"
        f"{hero}"
        f"<div class='dt-intro-cap'>{escape(cap)}</div>"
        f"</div>"
    )


def _cover_preload_html(cards: list[dict[str, Any]]) -> str:
    """首屏封面的下载在 hero 这块就发起，不等卡片 DOM。

    实测 hero 出现约 0.86s、卡片出现约 1.98s：脚本中间还要跑一千多毫秒，
    等 .pc-face-N 的 background-image 生效才发起请求，12 张约 1.5MB 就正好空着。

    href 必须和 _card_photo_css 走同一个 _cover_url，尺寸对不上就等于白预加载一遍。
    """
    return "".join(
        f'<link rel="preload" as="image" href="{escape(_cover_url(item["img"]), quote=True)}">'
        for item in cards
        if item.get("img")
    )


def _cover_url(url: str) -> str:
    """卡片封面按翻页后的实际格子宽度要图。

    数据里存的是 w=900（12 张一屏的时代），翻页之后一列只有 ~350px，
    24 张还按 900 拿就是白吃一倍流量，首屏比改版前更慢。
    """
    if not url:
        return url
    base, _, query = url.partition("?")
    params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
    params.update({"w": "720", "q": "72"})
    return base + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))


def _card_photo_css(cards: list[dict[str, Any]]) -> str:
    """照片按浮层序号挂到各自的 .pc-face-N 上，卡片换一批就换一份规则。"""
    rules = []
    for slot, item in enumerate(cards):
        src = _cover_url(item["img"])
        rules.append(f".pc-face-{slot} {{ background-image: url('{src}'); }}")
    return "<style>" + "\n".join(rules) + "</style>"


def _gallery(city: str, province: str, cover: str) -> list[str]:
    """详情图集：本市图组优先，不够再用本省补，最后盖上卡片封面，去重取 6 张。"""
    pool: list[str] = []
    for urls in (_bg_urls(city), _bg_urls(province), [cover]):
        for url in urls:
            if url not in pool:
                pool.append(url)
    return pool[:6]


def _video_box(name: str, video: dict[str, str]) -> None:
    """有 BV 就内嵌 B 站播放器，没有就给一条站内搜索链接，别开天窗。"""
    if video:
        st.markdown(
            "<div class='dt-video'><iframe src='https://player.bilibili.com/player.html?"
            f"bvid={quote(video['bv'])}&isWatchLater=1&autoplay=0&danmaku=0&high_quality=1' "
            "scrolling='no' frameborder='0' allowfullscreen></iframe></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div class='dt-link'>🎬 {escape(video.get('title') or '')}"
            f"　·　来源 {escape(video.get('author') or 'B 站')}　"
            "<a href='https://www.bilibili.com/video/"
            f"{quote(video['bv'])}' target='_blank'>在 B 站打开</a></div>",
            unsafe_allow_html=True,
        )
        return
    search = f"https://search.bilibili.com/all?keyword={quote(f'{_stem(name)} 旅游')}"
    st.markdown(
        "<div class='dt-video' style='display:flex;align-items:center;justify-content:center;'>"
        f"<a class='dt-link' href='{search}' target='_blank' style='color:#BFDBFE;font-size:14px;'>"
        f"🎬 还没有剪好这个地方的宣传片，点这里去 B 站搜「{_stem(name)} 旅游」</a></div>",
        unsafe_allow_html=True,
    )


_STOP_SEP = re.compile(r"[+＋、,，/／]|→")


def _stops_of(preset: dict[str, Any]) -> list[str]:
    """主点优先取卡片显式写的 stops；没写过的卡（比如以后新加的）才退回抠 desc。

    退回的那条路不读 payload['request']——那是「想走经典路线」「尽量少走路」这种用户诉求，
    混进看点列表就成了假景点。
    """
    explicit = preset.get("stops")
    if explicit is not None:  # 卡片写过就用它，空列表也算写过——那是「真没点名地方」
        return [str(s) for s in explicit][:6]
    text = str(preset.get("desc") or "")
    out: list[str] = []
    for token in _STOP_SEP.split(text):
        bit = token.strip(" 　。")
        if 2 <= len(bit) <= 10 and bit not in out:
            out.append(bit)
    return out[:6]


def _related_cards(preset: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    """相关推荐：别的城市各取第一条，够填满底部一排且不重复当前这条。"""
    city = str(preset["payload"].get("city") or "")
    pool: list[dict[str, Any]] = []
    for one in CITY_PRIORITY:
        if one == city or one not in CITY_CARDS:
            continue
        pool.append(CITY_CARDS[one][0])
        if len(pool) >= limit:
            break
    return pool


def _render_detail(preset: dict[str, Any]) -> None:
    """详情正文：左栏叙事（图集 / 视频 / 逐日骨架），右栏事实（要点 / 看点 / 适合谁），底部相关推荐。"""
    row = preset["payload"]
    city = str(row.get("city") or "")
    country, province = _locate_city(city)
    name = city or province
    people = int(row.get("people") or 0)
    budget = float(row.get("budget") or 0)
    days = int(row.get("days") or 0)

    cover = str(preset.get("img") or "")
    if cover:
        st.markdown(
            "<div class='dt-hero'>"
            f"<img src='{escape(cover, quote=True)}' alt=''>"
            "<div class='dt-hero-shade'></div>"
            "</div>",
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div class='dt-title'>{escape(preset['tag'])} {escape(preset['title'])}</div>"
        f"<div class='dt-desc'>{escape(preset['desc'])}</div>",
        unsafe_allow_html=True,
    )

    # 弹窗自带的 ✕ 太小，明确给一个「返回首页」：清掉行程数据重跑一轮，
    # 弹窗不再被打开，页面回到首页空态。key 沿用 detail_back，既有用例仍能点。
    if full_width_button("← 返回首页", key="detail_back"):
        _go_home()

    # 出发地要先实例化再读值：同一轮里读到的是刚选中的那个，片头的航线才画得对。
    ocol, _bcol = st.columns([1, 2.2], gap="small")
    with ocol:
        seed_state("f_origin", ORIGIN_DEFAULT)
        st.selectbox("出发地", ORIGIN_CITIES, key="f_origin")
    origin = str(st.session_state.get("f_origin") or ORIGIN_DEFAULT)
    # 原来这里是一段手绘 2D 矢量片头（_intro_html），现已由电影级 3D 开场取代。
    # 3D 片头只在「开始规划」跑完后播一次（见 _play_intro / _intro_pending），
    # 浏览详情页、换出发地都不会再触发，符合“只在开始规划时播放”的要求。
    _ = origin

    left, right = st.columns([1.55, 1], gap="large")

    with left:
        st.markdown("<div class='dt-sec'>这个地方长什么样</div>", unsafe_allow_html=True)
        photos = _gallery(city, province, str(preset.get("img") or ""))
        if photos:
            st.markdown(
                "<div class='dt-gal'>"
                + "".join(f"<img src='{url}' loading='lazy'>" for url in photos)
                + "</div>",
                unsafe_allow_html=True,
            )
        else:
            st.info("这个目的地还没收录实拍图组，先用上方卡片封面顶着。")

        st.markdown("<div class='dt-sec'>先看看视频</div>", unsafe_allow_html=True)
        _video_box(name, VIDEO_LIBRARY.get(name) or VIDEO_LIBRARY.get(province) or {})

        st.markdown("<div class='dt-sec'>大概怎么走</div>", unsafe_allow_html=True)
        stops = _stops_of(preset)
        per_day = max(1, -(-len(stops) // max(days, 1))) if stops else 0
        lines: list[str] = []
        for day in range(1, max(days, 1) + 1):
            chunk = stops[(day - 1) * per_day: day * per_day] if stops else []
            if day == 1:
                chunk = ["出发 / 抵达"] + chunk
            if day == max(days, 1):
                chunk = chunk + ["返程"]
            body = " → ".join(chunk) if chunk else "管家按你的偏好排布，规划后逐日生成"
            lines.append(
                f"<div class='dt-day'><span class='dt-dayno'>D{day}</span><span>{escape(body)}</span></div>"
            )
        st.markdown("<div class='dt-tl'>" + "".join(lines) + "</div>", unsafe_allow_html=True)

    with right:
        facts = [
            f"🗺️ {province} · {city}" if province and province != city else city or "全国",
            f"🗓️ {days} 天 {max(days - 1, 0)} 晚",
            f"👥 {people} 人",
            f"💰 总预算 {budget:.0f} 元" + (f"（人均 {budget / people:.0f}）" if people and budget else ""),
        ]
        if days and budget:
            facts.append(f"📅 日均预算约 {budget / days:.0f} 元")
        tags = [str(tag) for tag in (row.get("companions") or [])]
        if tags:
            facts.append("🧑‍🤝‍🧑 " + "、".join(tags))
        if row.get("preferences"):
            prefs = row["preferences"]
            shown = "、".join(str(p) for p in prefs) if isinstance(prefs, (list, tuple, set)) else str(prefs)
            facts.append("🎯 " + shown)
        st.markdown(
            "<div class='dt-panel'><div class='dt-sec'>行程要点</div><div class='dt-facts'>"
            + "".join(f"<span class='dt-fact'>{escape(fact)}</span>" for fact in facts)
            + "</div></div>",
            unsafe_allow_html=True,
        )
        if stops:
            st.markdown(
                "<div class='dt-panel'><div class='dt-sec'>主要看点</div>"
                + "".join(f"<div class='dt-li'>· {escape(s)}</div>" for s in stops)
                + "</div>",
                unsafe_allow_html=True,
            )
        # 每天几个点是拿文案抠出来的看点数除天数，只能当「满不满」的旁证：
        # 川西那种长途自驾卡抠出 1 个点/天，实际是特种兵。所以这里两个信号都看。
        tight = "特种兵拉练" in str(row.get("preferences") or "") or per_day >= 3
        who = "、".join(
            (t.split(" ", 1)[1] if " " in t else t) for t in tags[:2]
        ) + ("等" if len(tags) > 2 else "")
        who = who or "两人同行"
        fit = [
            f"适合{who}，{f'全程 {len(stops)} 个主点' if stops else '主点等规划时逐日排'}",
            "节奏偏满，得愿意早起晚归" if tight else "节奏偏松，睡到自然醒再出门",
        ]
        # 「你说过」只能引用用户真打过的字：ask_lines 有内容才代表他开过口。
        # 首屏直接点卡进来的，那句 request 是卡片自带的文案，别算在用户头上。
        spoke = bool(st.session_state.get("ask_lines"))
        quote = str((st.session_state.get("f_request") if spoke else row.get("request")) or "").strip()
        quote = (quote[:26] + "…") if len(quote) > 26 else quote
        said_as = "你说过" if spoke else "这条线的设定"
        st.markdown(
            "<div class='dt-panel'><div class='dt-sec'>这趟适合谁</div>"
            + "".join(f"<div class='dt-fit'>{escape(line)}</div>" for line in fit)
            + (f"<div class='dt-fit dt-fit-quote'>{said_as}：{escape(quote)}</div>" if quote else "")
            + "</div>",
            unsafe_allow_html=True,
        )

        if full_width_button("🚀 就按这个行程规划", kind="primary", key=f"go_{preset['key']}"):
            st.session_state["_open_detail"] = ""
            st.session_state["_pending_preset"] = preset
            st.session_state["_run_preset"] = True
            st.rerun()
        if full_width_button("✏️ 只套用，我先改条件", key=f"fill_{preset['key']}"):
            st.session_state["_open_detail"] = ""
            st.session_state["_pending_preset"] = preset
            st.rerun()

    related = _related_cards(preset)
    if related:
        st.markdown("<div class='dt-sec'>换个地方也行</div>", unsafe_allow_html=True)
        cols = st.columns(len(related), gap="small")
        for slot, (col, item) in enumerate(zip(cols, related)):
            with col:
                st.markdown(_related_face(item, slot), unsafe_allow_html=True)
                if full_width_button(f"看「{_stem(str(item['payload'].get('city') or ''))}」这条", key=f"rel_{item['key']}"):
                    # 推荐位只是换一张卡继续看详情。这里绝不能写 _run_preset：
                    # 一旦写了，点「看这条」就等于点「开始规划」，弹窗会被大模型请求顶掉。
                    st.session_state["_open_detail"] = item["key"]
                    st.rerun()


def show_detail(preset: dict[str, Any]) -> None:
    """新版用弹窗；老版本没有 st.dialog，退回在卡片下方就地展开，功能不缩水。"""
    dialog = getattr(st, "dialog", None)
    if callable(dialog):
        try:
            @dialog("目的地详情", width="large")
            def _box(item: dict[str, Any]) -> None:
                _render_detail(item)

            _box(preset)
            return
        except TypeError:
            pass
    with popover_or_expander(f"📍 {preset['title']} 详情"):
        _render_detail(preset)


def find_card(key: str) -> dict[str, Any] | None:
    for group in CITY_CARDS.values():
        for item in group:
            if item["key"] == key:
                return item
    return None


def render_open_detail() -> None:
    """详情弹窗由 _open_detail 驱动，而不是在按钮回调里当场打开。

    只有这样「看『xx』这条」才能只换一张卡继续看详情，「返回」才能真的关掉，
    而不是像以前那样点推荐位顺手把 AI 规划也触发了。
    """
    key = str(st.session_state.get("_open_detail") or "")
    if not key:
        return
    preset = find_card(key)
    if preset is None:
        st.session_state["_open_detail"] = ""
        return
    show_detail(preset)



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
    _PlanJob 会用 agent_payload() 裁掉它们，保证送给 Agent 的仍是原来那 8 个。
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
        step=None,  # 不显示加减号：想填多少直接敲
        key=k("people"),
        help="点输入框直接键入数字；公司团建等大团体可填更大人数",
    )

    seed_state(k("budget"), base["budget"])
    budget = st.number_input(
        "总预算（元）",
        min_value=200,
        max_value=9999999,
        step=None,  # 不显示加减号：三千块不必点三十下
        key=k("budget"),
        help="点输入框直接键入数字，例如 3000",
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


# ---------------------------------------------------------------------------
# 问答式约束输入：一句话 -> 结构化条件（规则解析打底，DeepSeek 补全）
# ---------------------------------------------------------------------------
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d{1,4}|[一二两三四五六七八九十]{1,3})"
_UNIT_MULT = {"万": 10000, "千": 1000, "w": 10000, "k": 1000}

# 口语里说「扬州」而不是「扬州市」：两套别名都建，命中取最长的那个
_CITY_ALIAS: dict[str, tuple[str, str, str]] = {}
_PROV_ALIAS: dict[str, tuple[str, str]] = {}


def _stem(name: str) -> str:
    """去掉行政区后缀：扬州市 -> 扬州，广西壮族自治区 -> 广西。"""
    for suffix in ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区"):
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            return name[: -len(suffix)]
    if name.endswith(("省", "市", "县", "州")) and len(name) > 2:
        return name[:-1]
    return name


def _build_aliases() -> None:
    for country, provinces in DESTINATION_TREE.items():
        _CITY_ALIAS[country.strip()] = (country, "", "")
        for province, cities in provinces.items():
            _PROV_ALIAS[province] = (country, province)
            if _stem(province) != province:
                _PROV_ALIAS[_stem(province)] = (country, province)
            for city in cities:
                _CITY_ALIAS[city] = (country, province, city)
                if _stem(city) != city:
                    _CITY_ALIAS[_stem(city)] = (country, province, city)


_build_aliases()

# 同行人 / 偏好：口语关键词 -> 带 Emoji 的控件选项
_COMPANION_KEYS: list[tuple[str, list[str]]] = [
    ("👤 独自出行", ["一个人", "独自一人", "自己一个", "一个人玩", "独自", "solo"]),
    ("👩 ❤️ 👨 情侣/夫妻", ["老婆", "老公", "女朋友", "男朋友", "男友", "女友", "对象",
                             "情侣", "夫妻", "蜜月", "未婚妻", "两个人一起"]),
    ("👴 带老人", ["老人", "爸妈", "父母", "爸爸", "妈妈", "爷爷", "奶奶", "外公", "外婆", "二老", "长辈"]),
    ("👶 带小孩", ["小孩", "孩子", "娃", "宝宝", "儿子", "女儿", "小朋友"]),
    ("👨 👩 👧 👦 亲子家庭", ["亲子", "一家三口", "一家四口", "全家", "一家人"]),
    ("👯 朋友结伴", ["朋友", "同学", "闺蜜", "兄弟", "伙伴"]),
    ("👨 💻 公司团建", ["团建", "公司", "部门", "同事"]),
    ("🐶 携带宠物", ["宠物", "狗", "猫", "毛孩子"]),
    ("🤰 孕妇", ["怀孕", "孕妇", "孕期", "准妈妈"]),
    ("♿ 需要无障碍设施", ["轮椅", "无障碍", "行动不便"]),
]
_PREFERENCE_KEYS: list[tuple[str, list[str]]] = [
    ("🚶 少走路/休闲游", ["少走路", "休闲", "轻松", "慢一点", "节奏慢", "不赶", "躺平", "度假"]),
    ("🏛️ 室内为主", ["室内", "博物馆", "展览", "美术馆", "避雨", "下雨"]),
    ("📸 出片率高", ["拍照", "出片", "网红", "摄影", "机位", "好看的照片"]),
    ("🍲 美食打卡", ["吃", "美食", "小吃", "餐厅", "菜", "馆子", "夜宵", "烧烤", "馆子"]),
    ("🏔️ 户外自然", ["山", "湖", "海", "森林", "草原", "徒步", "爬山", "自然", "露营", "日出", "星空"]),
    ("🎢 特种兵拉练", ["特种兵", "密集", "拉练", "赶一赶", "一天多个"]),
    ("🛍️ 购物逛街", ["购物", "逛街", "买买买", "免税", "奥特莱斯", "商圈"]),
    ("🎭 文化历史", ["历史", "文化", "古迹", "古镇", "古城", "老建筑", "非遗", "寺庙", "园林"]),
]
_FIXED_FESTIVALS = {"元旦": (1, 1), "五一": (5, 1), "劳动节": (5, 1), "国庆": (10, 1), "十一": (10, 1)}
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _cn_int(token: str) -> int | None:
    """把「8 / 八 / 十二 / 二十」这类写成整数；看不懂返回 None。"""
    token = str(token or "").strip()
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        return _CN_DIGITS.get(head, 1) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    return _CN_DIGITS.get(token)


def _money(token: str, unit: str) -> float | None:
    value = _cn_int(token)
    if value is None:
        return None
    return value * _UNIT_MULT.get(unit, 1)


def _pick_name(text: str, table: dict[str, Any]) -> Any | None:
    """按别名长度从长到短匹配，避免「浙江」抢在「浙江省」前面、「蒙古」抢在「内蒙古」前面。"""
    for alias in sorted(table, key=len, reverse=True):
        if len(alias) >= 2 and alias in text:
            return table[alias]
    return None


def _parse_start(text: str, today: date) -> date | None:
    """出发日期：X月Y日 / 明天后天 / 下周几 / 国庆五一元旦。"""
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            year = today.year + 1 if (month, day) < (today.month, today.day) else today.year
            try:
                return date(year, month, day)
            except ValueError:
                return None
    for word, offset in (("大后天", 3), ("后天", 2), ("明天", 1), ("今天", 0)):
        if word in text:
            return today + timedelta(days=offset)
    m = re.search(r"(?:下|这|本)?\s*(?:周|星期|礼拜)([一二三四五六日天])", text)
    if m:
        target = _WEEKDAYS[m.group(1)]
        delta = (target - today.weekday()) % 7 or 7
        if "下" in text:
            delta += 7 if (target - today.weekday()) % 7 else 0
        return today + timedelta(days=delta)
    for word, (month, day) in _FIXED_FESTIVALS.items():
        if word in text:
            year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
            return date(year, month, day)
    return None


def _parse_dates(text: str, today: date) -> tuple[date | None, date | None]:
    """X月Y日 出现两次就当成「几号到几号」，第二次是返程；只出现一次就只定出发。"""
    pairs: list[tuple[int, int]] = []
    month: int | None = None
    for found in re.finditer(r"(?:(\d{1,2})\s*月)?\s*(\d{1,2})\s*[日号]", text):
        if found.group(1):
            month = int(found.group(1))
        day = int(found.group(2))
        if month and 1 <= month <= 12 and 1 <= day <= 31:
            pairs.append((month, day))
    if not pairs:
        return _parse_start(text, today), None

    parsed: list[date] = []
    for mon, day in pairs:
        ref = parsed[-1] if parsed else today
        year = ref.year + 1 if (mon, day) < (ref.month, ref.day) else ref.year
        try:
            parsed.append(date(year, mon, day))
        except ValueError:
            continue
    if not parsed:
        return _parse_start(text, today), None
    return parsed[0], parsed[1] if len(parsed) > 1 else None


def parse_plan_text(text: str) -> dict[str, Any]:
    """规则解析：只返回这句话里确实说到的字段，没说的键不出现（增量覆盖靠调用方）。"""
    raw = str(text or "").strip()
    found: dict[str, Any] = {}
    if not raw:
        return found

    place = _pick_name(raw, _CITY_ALIAS)
    if place and place[2]:
        found["country"], found["province"], found["city"] = place
    else:
        prov = _pick_name(raw, _PROV_ALIAS)
        if prov:
            found["country"], found["province"] = prov

    m = re.search(rf"{_NUM}\s*(?:天|晚|日游)", raw)
    if m:
        days = _cn_int(m.group(1))
        if days:
            found["days"] = max(1, days)
    elif "一日游" in raw:
        found["days"] = 1

    m = re.search(rf"人均\s*{_NUM}\s*(万|千|w|k)?", raw)
    if m:
        found["per_person"] = _money(m.group(1), m.group(2) or "")
    m = re.search(rf"(?:预算|花费|费用|每人|人均)?\s*(?:不超过|控制在|大概|约)?\s*{_NUM}\s*(万|千|w|k)?\s*(?:元|块|块钱)", raw)
    if m and found.get("per_person") is None:
        found["budget"] = _money(m.group(1), m.group(2) or "")
    m = re.search(rf"预算\s*{_NUM}\s*(万|千|w|k)?", raw)
    if m and found.get("per_person") is None:
        found["budget"] = _money(m.group(1), m.group(2) or "")

    m = re.search(rf"{_NUM}\s*(?:个人|人(?!均)|位|个(?:人|朋友|同学|伙伴))", raw)
    if m:
        people = _cn_int(m.group(1))
        if people:
            found["people"] = max(1, people)
    if "一家三口" in raw:
        found["people"] = 3
    if "一家四口" in raw:
        found["people"] = 4

    start, end = _parse_dates(raw, date.today())
    if start:
        found["start"] = start
    if end:
        found["end"] = end

    picked = [opt for opt, keys in _COMPANION_KEYS if any(word in raw for word in keys)]
    if picked:
        found["companions"] = picked
    picked = [opt for opt, keys in _PREFERENCE_KEYS if any(word in raw for word in keys)]
    if picked:
        found["prefs"] = picked
    return found


_LLM_PARSE_HINT = (
    "你是旅行需求解析器，把用户的口语需求抽成 JSON，只输出 JSON 本身。"
    "字段：city(城市名)、province(省份名)、days(整数天数)、people(整数人数)、"
    f"budget(总预算数字，人民币)、per_person(人均预算数字)、start_date(YYYY-MM-DD)、"
    f"companions(数组，只能取：{'、'.join(COMPANION_LABELS)})、"
    f"preferences(数组，只能取：{'、'.join(PREFERENCE_LABELS)})。"
    "用户没说的字段一律填 null，不要替人猜；相对日期请换算成具体日期。"
)


def _llm_plan(text: str) -> dict[str, Any] | None:
    """让 DeepSeek 补规则解析漏掉的说法；没配密钥或失败就返回 None，规则结果照常生效。"""
    if not config.DEEPSEEK_API_KEY:
        return None
    try:
        message, _ = llm.chat(
            [
                {"role": "system", "content": f"{_LLM_PARSE_HINT} 今天是 {date.today().isoformat()}。"},
                {"role": "user", "content": text},
            ],
            temperature=0.0,
            max_tokens=300,
        )
        body = re.search(r"\{.*\}", str(message.get("content") or ""), re.S)
        if not body:
            return None
        data = json.loads(body.group(0))
    except Exception:  # noqa: BLE001 - 解析失败退回规则结果，不打断对话
        return None

    found: dict[str, Any] = {}
    if data.get("city"):
        city = str(data["city"])
        country, province = _locate_city(city)
        found["country"], found["province"], found["city"] = country, province, city
    elif data.get("province"):
        hit = _pick_name(str(data["province"]), _PROV_ALIAS)
        if hit:
            found["country"], found["province"] = hit
    for key in ("days", "people", "budget", "per_person"):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > 0:
            found[key] = float(value) if key == "budget" else int(value)
    raw_date = str(data.get("start_date") or "")
    if len(raw_date) == 10:
        try:
            found["start"] = date.fromisoformat(raw_date)
        except ValueError:
            pass
    companions = [_companion_option(tag) for tag in (data.get("companions") or [])]
    if any(companions):
        found["companions"] = [c for c in companions if c]
    prefs = [_preference_option(tag) for tag in (data.get("preferences") or [])]
    if any(prefs):
        found["prefs"] = [p for p in prefs if p]
    return found or None


def _merge_plan(rule: dict[str, Any], model: dict[str, Any] | None) -> dict[str, Any]:
    """规则结果打底，模型结果补空缺；两边都提到的取模型（它更懂上下文），列表取并集。"""
    merged = dict(rule)
    for key, value in (model or {}).items():
        if isinstance(value, list):
            merged[key] = list(dict.fromkeys(list(merged.get(key) or []) + value))
        elif key not in merged:
            merged[key] = value
    return merged


def _patch_state(found: dict[str, Any]) -> None:
    """把解析结果增量写进 f_* 状态（只能在控件实例化前调用）。"""
    if not found:
        return
    today = date.today()
    if found.get("city"):
        country, province = found.get("country"), found.get("province")
        if not (country and province):
            country, province = _locate_city(found["city"])
        st.session_state["f_country"] = country
        st.session_state["f_province"] = province
        st.session_state["f_city"] = found["city"]
    elif found.get("province"):
        st.session_state["f_country"] = found["country"]
        st.session_state["f_province"] = found["province"]
        st.session_state["f_city"] = UNLIMITED_CITY

    people = found.get("people") or st.session_state.get("f_people", CONSTRAINT_DEFAULTS["people"])
    if found.get("per_person"):
        st.session_state["f_budget"] = int(round(float(found["per_person"]) * max(1, int(people))))
    elif found.get("budget"):
        st.session_state["f_budget"] = int(round(float(found["budget"])))
    if found.get("people"):
        st.session_state["f_people"] = int(found["people"])

    days = found.get("days")
    start = found.get("start")
    end = found.get("end")
    if start:
        st.session_state["f_start"] = start
    if end:
        st.session_state["f_end"] = end
    elif days and start:
        st.session_state["f_end"] = start + timedelta(days=int(days) - 1)
    elif days and isinstance(st.session_state.get("f_start"), date):
        st.session_state["f_end"] = st.session_state["f_start"] + timedelta(days=int(days) - 1)

    for index, option in enumerate(COMPANION_OPTIONS):
        key = f"f_companions_{index}"
        if option in (found.get("companions") or []):
            st.session_state[key] = True
    for index, option in enumerate(PREFERENCE_OPTIONS):
        key = f"f_prefs_{index}"
        if option in (found.get("prefs") or []):
            st.session_state[key] = True


def _state_snapshot() -> dict[str, Any]:
    """控件还没渲染时，从 session_state 读出一份当前约束用于复述。"""
    start = st.session_state.get("f_start")
    end = st.session_state.get("f_end")
    days = _span_days(start, end) if isinstance(start, date) and isinstance(end, date) else 0
    return {
        "country": st.session_state.get("f_country", CONSTRAINT_DEFAULTS["country"]),
        "province": st.session_state.get("f_province", CONSTRAINT_DEFAULTS["province"]),
        "city": st.session_state.get("f_city", CONSTRAINT_DEFAULTS["city"]),
        "start": start,
        "end": end,
        "days": days,
        "people": st.session_state.get("f_people", CONSTRAINT_DEFAULTS["people"]),
        "budget": st.session_state.get("f_budget", CONSTRAINT_DEFAULTS["budget"]),
        "companions": _current_group("f_", "companions", COMPANION_OPTIONS),
        "prefs": _current_group("f_", "prefs", PREFERENCE_OPTIONS),
        "request": st.session_state.get("f_request", CONSTRAINT_DEFAULTS["request"]),
    }


def _payload_from_state() -> dict[str, Any]:
    """底部输入卡在脚本末尾渲染，规划那一轮要用的条件只能从 session_state 取。

    字段与 _constraint_fields() 的返回保持一致，只是来源换成状态而不是控件返回值。
    """
    snap = _state_snapshot()
    prefs = list(snap["prefs"])
    labels = [
        PREFERENCE_LABELS[PREFERENCE_OPTIONS.index(p)]
        for p in prefs
        if p in PREFERENCE_OPTIONS
    ]
    note = str(st.session_state.get("f_pref_note", CONSTRAINT_DEFAULTS["pref_note"]) or "").strip()
    pref_text = "、".join(labels)
    if note:
        pref_text = f"{pref_text}；{note}" if pref_text else note

    min_date, max_date = _date_bounds()
    start = snap["start"] if isinstance(snap["start"], date) else date.today() + timedelta(days=1)
    end = snap["end"] if isinstance(snap["end"], date) else start
    end = max(start, min(end, max_date))

    unlimited = (UNLIMITED_PROVINCE, UNLIMITED_CITY)
    return {
        "country": snap["country"],
        "province": "" if snap["province"] in unlimited else snap["province"],
        "city": "" if snap["city"] in unlimited else snap["city"],
        "days": _span_days(start, end),
        "people": int(snap["people"] or CONSTRAINT_DEFAULTS["people"]),
        "budget": float(snap["budget"] or CONSTRAINT_DEFAULTS["budget"]),
        "start_date": start.isoformat(),
        "end": end.isoformat(),
        "companions": plain_companions(snap["companions"]),
        "preferences": pref_text,
        "preferences_list": prefs,
        "preferences_note": note,
        "request": snap["request"],
    }


def _reply_text(found: dict[str, Any], snapshot: dict[str, Any], *, used_model: bool) -> str:
    """管家复述：把「我听懂了什么」和「还差什么」一次说清，用户只管纠正。"""
    city = snapshot["city"]
    lines: list[str] = []
    if city and city != UNLIMITED_CITY:
        where = f"**{city}**（{snapshot['province']}）"
    elif snapshot["province"] != UNLIMITED_PROVINCE:
        where = f"**{snapshot['province']}**，城市还没定"
    else:
        where = "全国范围，目的地还没定"
    lines.append(f"- 去哪：{where}")
    if snapshot["days"]:
        lines.append(
            f"- 时间：{snapshot['start']:%m月%d日} 出发，玩 {snapshot['days']} 天"
            f"（{snapshot['end']:%m月%d日} 返程）"
        )
    else:
        lines.append("- 时间：还没定，先按默认日期占位")
    people = int(snapshot["people"] or 0)
    budget = float(snapshot["budget"] or 0)
    per = f"，人均约 {budget / people:.0f}" if people and budget else ""
    lines.append(f"- 人数与预算：{people} 人 · 总预算 {budget:.0f} 元{per}")
    tags = [t.split(" ", 1)[1] if " " in t else t for t in snapshot["companions"]]
    lines.append(f"- 同行人：{'、'.join(tags)}" if tags else "- 同行人：还没说（默认按 2 人成人安排）")
    prefs = [t.split(" ", 1)[1] if " " in t else t for t in snapshot["prefs"]]
    lines.append(f"- 偏好：{'、'.join(prefs)}" if prefs else "- 偏好：还没提到")

    gaps: list[str] = []
    if not city or city == UNLIMITED_CITY:
        gaps.append("具体城市")
    if not found.get("start"):
        gaps.append("出发日期")
    if not found.get("days"):
        gaps.append("玩几天")
    tail = f"还差点信息：{'、'.join(gaps)}。" if gaps else "条件齐了，可以直接开始规划。"
    who = "我让模型又核了一遍，" if used_model and config.DEEPSEEK_API_KEY else ""
    return (
        "好的，" + who + "我按这样理解：\n\n"
        + "\n".join(lines)
        + f"\n\n{tail}要改就直接说（例如「预算改成 6000」「带上爸妈」），"
        "或者点「精确设置」逐项勾。"
    )


def _chat_log() -> list[dict[str, str]]:
    """管家与用户的对话记录。开场白不再入日志：输入卡自己已经说明了怎么用。"""
    return st.session_state.setdefault("chat_log", [])


def apply_pending_chat() -> None:
    """消费上一轮暂存的对话：解析 -> 写回条件 -> 生成管家回复（必须在控件实例化前）。"""
    text = st.session_state.pop("_pending_chat", None)
    if not text:
        return
    # 用户开口了就把详情关掉，管家的话别压在弹窗底下
    st.session_state["_open_detail"] = ""
    log = _chat_log()
    log.append({"role": "user", "content": str(text)})

    rule = parse_plan_text(str(text))
    model = _llm_plan(str(text))
    found = _merge_plan(rule, model)
    _patch_state(found)

    lines = st.session_state.setdefault("ask_lines", [])
    lines.append(str(text))
    st.session_state["f_request"] = "；".join(lines[-3:])

    log.append({"role": "assistant", "content": _reply_text(found, _state_snapshot(), used_model=bool(model))})


def render_composer() -> None:
    """页面底部吸底的旅行管家输入卡：精确设置 + 一句话 + 开始规划。

    对话气泡与「已套用」提示已移除，只留操作区。控件在脚本末尾执行，
    所以点按钮 / 提交话都走「写标记 + rerun」，下一轮在顶部用
    _payload_from_state() 取条件，保证只跑一次。
    """
    with st.container(key="composer"):
        with popover_or_expander("⚙️ 精确设置（不想打字就点这个）"):
            _constraint_fields("f_")

        bar = st.columns([4, 1], gap="small")
        with bar[0]:
            said = st.chat_input(
                "例如：10月15日和老婆去扬州玩3天，人均2000，想看园林吃淮扬菜",
                key="chat_said",
                submit_mode="disable",
            )
        with bar[1]:
            if full_width_button("开始规划", kind="primary"):
                st.session_state["_run_now"] = True
                st.rerun()
        if said:
            # 控件本轮已实例化，改状态只能等下一次运行，所以先暂存再 rerun
            st.session_state["_pending_chat"] = said
            st.rerun()



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


# ---------------------------------------------------------------------------
# 规划进度：trace 的真实阶段 -> 进度卡上的一行
# ---------------------------------------------------------------------------
PLAN_STAGES: tuple[tuple[str, str], ...] = (
    ("定位目的地", "确认城市中心，天气和路程都以它为准"),
    ("搜集景点与美食", "从地图数据里挑出值得去的地方和小吃"),
    ("对比这几天天气", "把户外安排放到不下雨的日子"),
    ("编排每日行程", "按时间和路程把景点串成顺路的线"),
    ("复核路线与预算", "检查时间冲突、点间路程和总花费"),
    # 最后一格在 run() 返回前不会打勾：校验通过后还要汇总预算、拼行程单、
    # 准备片头。留这一格，进度条就不会在真正完成前先跑到 100% 顶住。
    ("整理成行程单", "把每天的安排、预算和地图点位汇总起来"),
)

# trace 里的工具名 -> 阶段序号（与 PLAN_STAGES 的顺序一一对应）
_PLAN_TOOL_STAGE = {
    "geocode": 0,
    "search_poi": 1,
    "get_weather": 2,
    "plan_route": 3,
    "estimate_budget": 4,
}

# 没有工具名的条目：草稿解析 / 校验 / 预算汇总 / 最终完成。
# 「模型决策」（decision）故意不算：那时只是决定要调哪些工具，行程还没开始写。
_PLAN_KIND_STAGE = {
    "parse_error": 3,
    "truncated": 3,
    "validation": 4,
    "budget": 4,
    "done": 5,
}

# 可以直接展示给用户的 trace：这些行本身就是人话（不含坐标与参数名）。
# 其余类型（校验问题清单等）只取一句固定文案，不把「问题 2：营业时间…」丢给用户。
_PLAN_LIVE_KINDS = {"tool_call", "tool_result", "prepare", "cache", "constraint"}
_PLAN_LIVE_FALLBACK = {
    "decision": "正在挑下一个要查的地点…",
    "validation": "正在复核每天的时间与路程安排…",
    "issue": "发现一处时间冲突，正在重新排一次…",
    "parse_error": "行程草稿格式不对，正在让模型重写…",
    "truncated": "输出被截断，正在让模型精简重发…",
    "budget": "正在核算门票、交通与住宿花费…",
    "give_up": "自动重排已到上限，正在用当前最优方案收尾…",
    "done": "行程排好了，正在整理成行程单…",
}


def _plan_live_line(entry: dict[str, Any]) -> str:
    """挑一行能直接给用户看的进展；技术明细换成固定文案。"""
    kind = str(entry.get("kind") or "")
    if kind in _PLAN_LIVE_KINDS:
        return str(entry.get("line") or "").strip()
    return _PLAN_LIVE_FALLBACK.get(kind, "")


def _plan_stage_of(entry: dict[str, Any]) -> int | None:
    """trace 条目 -> 阶段序号；认不出就返回 None，绝不猜一个假阶段。"""
    if entry.get("kind") in ("tool_call", "tool_result"):
        stage = _PLAN_TOOL_STAGE.get(str(entry.get("tool") or ""))
        if stage is not None:
            return stage
    return _PLAN_KIND_STAGE.get(str(entry.get("kind") or ""))


def _plan_progress_html(done: set[int], live: str, elapsed: float) -> str:
    """把「已完成阶段 + 最新一行真实进展 + 已耗时」画成玻璃卡。

    宽度只取 len(done) / 阶段总数，不掺任何时间因子：54 秒的慢请求和 12 秒的
    快请求走到同一阶段时长得一样，不会出现「跑满卡住」。
    """
    total = len(PLAN_STAGES)
    current = next((i for i in range(total) if i not in done), total - 1)
    pct = max(4, round(len(done) / total * 100))

    rows = []
    for index, (title, hint) in enumerate(PLAN_STAGES):
        if index in done:
            state, mark = "is-done", "✓"
        elif index == current:
            state, mark = "is-live", ""
        else:
            state, mark = "", ""
        rows.append(
            f"<li class='ci-plan-step {state}'>"
            f"<span class='ci-plan-mark'>{mark}</span>"
            f"<span>{escape(title)}<span class='ci-plan-sub'> · {escape(hint)}</span></span>"
            f"</li>"
        )

    live_text = live or "已收到需求，正在准备地图与天气数据…"
    return (
        "<div class='ci-plan-card' role='status' aria-live='polite'>"
        "<div class='ci-plan-top'>"
        "<div class='ci-plan-title'><span class='ci-plan-dot'></span>正在为您规划行程</div>"
        f"<div class='ci-plan-clock'>已用 <b id='ci-plan-elapsed'>{elapsed:.1f}</b> 秒</div>"
        # 视觉按钮在这里；点击由 ticker 脚本转给隐藏的真实 st.button
        "<button type='button' class='ci-plan-cancel' data-ci-cancel='1'>取消规划</button>"
        "</div>"
        f"<div class='ci-plan-track'><span class='ci-plan-fill' style='width:{pct}%'></span></div>"
        "<div class='ci-plan-note'><span id='ci-plan-hint'>这一步通常最花时间，请稍候…</span>"
        "<span class='ci-plan-est'> · 进度按真实阶段推进，剩余时间无法精确预估</span></div>"
        "<ul class='ci-plan-steps'>" + "".join(rows) + "</ul>"
        f"<div class='ci-plan-live'><span class='ci-plan-tag'>最新</span>{escape(live_text)}</div>"
        "</div>"
    )


class _PlanJob:
    """一次后台规划任务。

    线程里只写普通属性（result / error / done / live），所有 st 调用都留在
    主脚本线程。这样进度卡和「取消规划」按钮始终能被 Streamlit 处理，不用
    等 run() 跑完才有响应。
    """

    def __init__(self, request: str, payload: dict[str, Any]) -> None:
        self.request = request
        self.payload = agent_payload(payload)
        self.started = time.monotonic()
        self.run_id = str(time.time_ns())
        self.done: set[int] = set()
        self.live = ""
        self.result: dict[str, Any] | None = None
        self.outline: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.finished = threading.Event()
        self.cancelled = threading.Event()
        self._thread: threading.Thread | None = None

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def on_trace(self, entry: dict[str, Any]) -> None:
        line = _plan_live_line(entry)
        if line:
            self.live = line
        stage = _plan_stage_of(entry)
        if stage is not None:
            # 走到后面的阶段说明前面的也真的做完了：一起打勾，避免倒序观感。
            self.done.update(range(stage + 1))

    def start(self) -> None:
        def work() -> None:
            try:
                engine = agent.TravelAgent(
                    trace_callback=self.on_trace,
                    cancelled=self.cancelled.is_set,
                )

                def _outline_ready(outline: dict[str, Any] | None) -> None:
                    if outline and (outline.get("itinerary") or {}).get("days"):
                        # 只写普通属性：所有 st.* 调用都留在主脚本线程
                        self.outline = outline

                self.result = engine.run_two_stage(self.request, self.payload, _outline_ready)
            except BaseException as exc:  # noqa: BLE001 - 异常交给主线程展示
                self.error = exc
            finally:
                self.finished.set()

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()


def _snapshot_form() -> None:
    """取消 / 返回首页时把表单条件留给下一轮控件初始化。

    规划、片头、详情页都不渲染 composer，Streamlit 会把 f_* 的控件状态当成
    过期控件回收，回首页时表单会变回默认值。这里用 _pending_constraints 把
    开跑时那份 payload 写回去，用户不用重新填一遍城市和日期。
    """
    last = st.session_state.get("_last_payload")
    if last:
        seed = dict(last)
        # _payload_from_state 里的 end 是 ISO 字符串，而 f_end 背后是 date_input，
        # 直接把字符串塞进 session_state 会让控件在下一轮抛类型错误。
        end = seed.get("end")
        if isinstance(end, str):
            try:
                seed["end"] = date.fromisoformat(end)
            except ValueError:
                seed.pop("end", None)
        st.session_state["_pending_constraints"] = seed


def _go_home() -> None:
    """清空本次行程数据，回到首页空态，并把筛选显式重置为「全国热门」。

    退出回调是脏状态的唯一入口：_snapshot_form() 只保存天数/人数/预算等，
    如果省份和城市跟着 _pending_constraints 原样回填，首页会变成刚才看的
    那个省市的推荐。这里在回填种子里写死三项，并在本轮先改 f_*：
    详情页 / 结果页不渲染 composer，f_* 控件未实例化，此时写入不会抛
    StreamlitAPIException。
    """
    _snapshot_form()
    seed = st.session_state.get("_pending_constraints")
    if isinstance(seed, dict):
        seed["country"] = CONSTRAINT_DEFAULTS["country"]
        seed["province"] = UNLIMITED_PROVINCE
        seed["city"] = UNLIMITED_CITY
    # 大纲态直接返回首页：先把后台线程停掉，别让它跑完再写回状态
    job = st.session_state.pop("_agent_job", None)
    if job is not None:
        job.cancelled.set()
    st.session_state["f_country"] = CONSTRAINT_DEFAULTS["country"]
    st.session_state["f_province"] = UNLIMITED_PROVINCE
    st.session_state["f_city"] = UNLIMITED_CITY
    st.session_state["_card_page"] = 1
    for key in (
        "result",
        "_intro_pending",
        "_intro_result",
        "_intro_played",
        "_open_detail",
        "_plan_error",
        "_pdf_cache",
        "_outline_diff_note",
    ):
        st.session_state.pop(key, None)
    st.rerun()


def _start_plan(request: str, payload: dict[str, Any]) -> bool:
    """开一个后台规划任务；城市没填好就留在首页提示，不建任务。"""
    if not str(payload.get("city") or "").strip():
        st.warning("还缺一个具体城市就能开跑，点下方「精确设置」选一个，或直接告诉我去哪。")
        return False
    st.session_state["_last_payload"] = dict(payload)
    for key in ("_intro_played", "_intro_pending", "_intro_result", "_outline_diff_note"):
        st.session_state.pop(key, None)
    job = _PlanJob(request, payload)
    job.start()
    st.session_state["_agent_job"] = job
    return True


def _outline_diff_note(outline: dict[str, Any], result: dict[str, Any]) -> str:
    """对比大纲与校验后的终稿，告诉用户哪里被改过（不静默替换）。"""

    def signature(container: dict[str, Any]) -> set[tuple[int, str, str]]:
        marks: set[tuple[int, str, str]] = set()
        for day in (container.get("itinerary") or {}).get("days") or []:
            for item in day.get("items") or []:
                if item.get("type") != "attraction":
                    continue
                marks.add(
                    (
                        int(day.get("day") or 0),
                        str(item.get("name") or ""),
                        str(item.get("start") or ""),
                    )
                )
        return marks

    changed = len(signature(outline) ^ signature(result))
    if not changed:
        return ""
    return f"校验后行程有 {changed} 处调整（点位或时间），下方为最终版本。"


def _finish_plan_job(job: _PlanJob) -> None:
    """后台任务收尾：成功落到 result，失败转成首页上的一句错误。

    被取消的旧任务即使晚到，也不能覆盖当前会话的新任务。
    """
    if st.session_state.get("_agent_job") is not job:
        return
    st.session_state.pop("_agent_job", None)
    if job.error is not None:
        # 版本错位兜底：app.py 先上线、agent.py 还没跟上时，agent 模块里
        # 可能没有 AgentCancelled。这里用 getattr 退回「非取消」，让界面落到
        # 普通错误文案，而不是 isinstance 直接 AttributeError 整页崩红。
        if isinstance(job.error, getattr(agent, "AgentCancelled", ())):
            return
        if isinstance(job.error, agent.AgentError):
            st.session_state["_plan_error"] = f"这次没能规划成功：{zh_labels.friendly_error(job.error)}"
        else:
            st.session_state["_plan_error"] = "出了点小状况，请稍后再试一次。"
        return
    result = job.result or {}
    st.session_state["result"] = result
    if job.outline:
        note = _outline_diff_note(job.outline, result)
        if note:
            st.session_state["_outline_diff_note"] = note
    # 只有「真的跑完一次规划」才放行片头；失败时不播，避免先开场再落进空详情页。
    # 两段式里片头已经在大纲阶段播过，就不要再播一次。
    if (result.get("itinerary") or {}).get("days") and not st.session_state.get("_intro_played"):
        st.session_state["_intro_pending"] = True


def _render_plan_job(job: _PlanJob) -> None:
    """等待期：进度卡 + 取消按钮，用 fragment 局部刷新真实阶段。

    这里刻意不再整页 rerun。旧写法每 0.6s 重跑整个脚本，主区（背景层、
    卡片外壳）跟着反复重建；在 Tabbit 这类软件合成（Tiles 关闭）的环境里，
    每次重建都会把上一帧再提交一次，屏幕上就是"首页残影"闪一下。
    改成 fragment 后只有卡片这一小块重画，背景与图层树保持不动。

    fragment 内的取消与收尾都必须 scope="app"：局部 rerun 只会原地转圈。
    """
    if not _supports_fragment():
        _render_plan_job_legacy(job)
        return

    inject_css()
    _embed_plan_ticker(job.run_id)
    cancel_key = f"ci_plan_cancel_{job.run_id}"

    @st.fragment(run_every=0.6)
    def _tick() -> None:
        # 容器 key 固定为 ci_plan_wrap：卡片样式和 ticker 里「点 HTML 胶囊 ->
        # 点隐藏真按钮」的选择器都按这个 key 找落点，换路径不能换 key
        with st.container(key="ci_plan_wrap"):
            box = st.empty()
            box.markdown(
                _plan_progress_html(job.done, job.live, job.elapsed()),
                unsafe_allow_html=True,
            )
            if st.button("取消规划", key=cancel_key):
                job.cancelled.set()
                _snapshot_form()
                for key in ("result", "_intro_pending", "_intro_result", "_intro_played", "_open_detail", "_plan_error", "_pdf_cache", "_outline_diff_note"):
                    st.session_state.pop(key, None)
                st.session_state.pop("_agent_job", None)
                box.empty()
                st.rerun(scope="app")
        if job.finished.is_set():
            # 先收尾（成功落 result / 失败落 _plan_error），再整页切走：
            # 成功进片头，失败落到首页的错误条，都不在 fragment 里干等
            _finish_plan_job(job)
            box.empty()
            st.rerun(scope="app")
        if job.outline is not None:
            # 大纲先到了：立刻整页切到大纲页，细节继续在后台补
            box.empty()
            st.rerun(scope="app")

    _tick()
    # 外层脚本停在等待页，别再往下渲染空态
    st.stop()


_OUTLINE_CSS = """
<style>
  .ol-banner{
    display:flex;align-items:center;gap:12px;margin:6px 0 14px;padding:12px 16px;
    border-radius:14px;border:1px solid rgba(245,158,11,.55);
    background:linear-gradient(90deg,rgba(245,158,11,.24),rgba(245,158,11,.08));
    color:#fde68a;font-size:14px;backdrop-filter:blur(6px);
  }
  .ol-banner b{display:block;font-size:14px;margin-bottom:2px;color:#fffbeb}
  .ol-banner span{display:block;font-size:12px;color:#fcd34d;line-height:1.5}
  .ol-banner.ol-fail{
    border-color:rgba(248,113,113,.6);
    background:linear-gradient(90deg,rgba(220,38,38,.3),rgba(220,38,38,.1));
    color:#fecaca;
  }
  .ol-banner.ol-fail b{color:#fff}
  .ol-banner.ol-fail span{color:#fecaca}
  .ol-dot{width:10px;height:10px;border-radius:50%;background:#fbbf24;flex:0 0 auto;
    animation:ol-pulse 1.4s ease-in-out infinite}
  .ol-tag{margin-left:auto;flex:0 0 auto;padding:3px 10px;border-radius:999px;
    background:#f59e0b;color:#1f2937;font-weight:600;font-size:12px;letter-spacing:.08em}
  @keyframes ol-pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.75)}}
  .ol-skel{margin:8px 0 22px;padding:16px;border-radius:14px;
    border:1px dashed rgba(148,163,184,.5);background:rgba(15,23,42,.5)}
  .ol-skel-title{font-size:13px;color:#cbd5e1;margin-bottom:10px}
  .ol-skel-title em{font-style:normal;color:#fbbf24;margin-left:8px}
  .ol-line{height:12px;border-radius:6px;margin:8px 0;
    background:linear-gradient(90deg,rgba(148,163,184,.22) 25%,rgba(203,213,225,.46) 37%,rgba(148,163,184,.22) 63%);
    background-size:400% 100%;animation:ol-shimmer 1.6s ease infinite}
  .ol-line.w70{width:70%}.ol-line.w45{width:45%}.ol-line.w85{width:85%}
  @keyframes ol-shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
</style>
"""


def _outline_banner_html(job: "_PlanJob") -> str:
    failed = job.finished.is_set() and job.error is not None
    if failed:
        reason = (
            zh_labels.friendly_error(job.error)
            if isinstance(job.error, agent.AgentError)
            else "后台补全中断"
        )
        return (
            "<div class='ol-banner ol-fail'><div><b>细节补全失败，当前只是大纲</b>"
            f"<span>预算、点间路况、营业时间校验和备注都没有完成：{escape(reason)}。"
            "可以点「重新规划」再试一次。</span></div>"
            "<div class='ol-tag' style='background:#dc2626'>未完成</div></div>"
        )
    live = escape(job.live or "正在计算路况、预算与营业时间…")
    return (
        "<div class='ol-banner'><div class='ol-dot'></div><div><b>大纲已生成，正在补全细节</b>"
        f"<span>已用 {job.elapsed():.0f} 秒 · {live}</span></div>"
        "<div class='ol-tag'>大纲</div></div>"
    )


def _abort_outline_job(job: "_PlanJob") -> None:
    """大纲态返回/取消：先停线程，再清状态回首页，避免后台跑完又写回。"""
    job.cancelled.set()
    st.session_state.pop("_agent_job", None)
    _snapshot_form()
    _go_home()


def _render_outline_job(job: "_PlanJob") -> None:
    """大纲页：先让用户看到每天去哪，细节在后台补，完成后自动换成终稿。"""
    inject_css()
    st.markdown(_OUTLINE_CSS, unsafe_allow_html=True)

    outline = job.outline or {}
    days = (outline.get("itinerary") or {}).get("days") or []
    c = outline.get("constraints") or {}
    if job.finished.is_set() and job.error is None:
        _finish_plan_job(job)
        st.rerun()

    nav = st.columns([1, 1, 2.4])
    with nav[0]:
        if st.button("← 返回首页", key="outline_home"):
            _abort_outline_job(job)
    with nav[1]:
        if st.button("取消补全", key="outline_cancel"):
            _abort_outline_job(job)

    @st.fragment(run_every=0.8)
    def _status() -> None:
        st.markdown(_outline_banner_html(job), unsafe_allow_html=True)
        if job.finished.is_set() and job.error is None:
            _finish_plan_job(job)
            st.rerun(scope="app")
        if job.finished.is_set() and job.error is not None:
            if st.button("重新规划", key="outline_retry"):
                st.session_state.pop("_agent_job", None)
                _snapshot_form()
                st.session_state["_run_now"] = True
                st.rerun(scope="app")

    _status()

    st.markdown(
        f"#### {escape(str(c.get('city') or '行程'))} {int(c.get('days') or len(days))} 天 · 大纲"
    )
    st.caption("以下是 AI 先给出的每日点位。预算、点间路况、营业时间校验仍在后台计算，完成后页面会自动刷新为终稿。")

    head_cols = st.columns([3, 1])
    with head_cols[0]:
        st.subheader("行程一览")
    with head_cols[1]:
        st.button("📥 下载行程单 PDF", disabled=True, key="outline_pdf")
        st.caption("补全完成后可用：避免导出缺少预算与校验的半成品。")

    render_itinerary_table(days)
    if days:
        st.subheader("每日详情")
        for day in days:
            render_day_card(day)
    else:
        st.info("大纲里还没有可展示的点位，稍等片刻或返回重试。")

    st.markdown(
        "<div class='ol-skel'><div class='ol-skel-title'>预算与花费<em>计算中</em></div>"
        "<div class='ol-line w70'></div><div class='ol-line w45'></div>"
        "<div class='ol-line w85'></div></div>"
        "<div class='ol-skel'><div class='ol-skel-title'>行程要点<em>生成中</em></div>"
        "<div class='ol-line w85'></div><div class='ol-line w70'></div></div>",
        unsafe_allow_html=True,
    )


def _supports_fragment() -> bool:
    """1.66 起有 st.fragment(run_every=...)。缺了就退回旧轮询，不让整页崩。"""
    if not hasattr(st, "fragment"):
        return False
    try:
        st.fragment(run_every=0.6)
    except TypeError:
        return False
    except Exception:  # 某些版本在没有脚本上下文时抛别的异常
        return False
    return True


def _render_plan_job_legacy(job: _PlanJob) -> None:
    """降级路径：与改动前一致的整页轮询。"""
    inject_css()
    with st.container(key="ci_plan_wrap"):
        st.markdown(
            _plan_progress_html(job.done, job.live, job.elapsed()),
            unsafe_allow_html=True,
        )
        if st.button("取消规划", key="ci_plan_cancel"):
            job.cancelled.set()
            _snapshot_form()
            for key in ("result", "_intro_pending", "_intro_result", "_intro_played", "_open_detail", "_plan_error", "_pdf_cache", "_outline_diff_note"):
                st.session_state.pop(key, None)
            st.session_state.pop("_agent_job", None)
            st.rerun()
    _embed_plan_ticker(job.run_id)
    if job.finished.is_set():
        _finish_plan_job(job)
        st.rerun()
    time.sleep(0.6)
    st.rerun()


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

    # 详情页左上角：清掉这次行程直接回首页空态
    if st.button("← 返回首页", key="result_home"):
        _go_home()

    # 大纲到终稿如果有差异，明确告诉用户改了什么，而不是静默替换
    diff_note = st.session_state.get("_outline_diff_note")
    if diff_note:
        st.info(diff_note)

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
        try:
            pdf_bytes = _itinerary_pdf_bytes(result)
        except Exception:  # noqa: BLE001 - 导出失败不能把结果页整页打崩
            logging.getLogger("travel_agent.detail").exception("pdf_export_failed")
            st.warning("行程单 PDF 生成失败，请刷新后重试。")
        else:
            full_width_download(
                "📥 下载行程单 PDF",
                data=pdf_bytes,
                file_name=f"{c['city']}-{c['days']}天行程单.pdf",
                mime="application/pdf",
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
def _intro_pois(result: dict[str, Any]) -> list[dict[str, Any]]:
    """把 itinerary.days 里的「游玩」条目拍平成片头要的 POI 列表。

    只取 attraction（吃饭/住宿不进开场地图）；坐标沿用高德 GCJ-02，
    组件侧按 config.coordShift 统一纠偏；停留时长在这里折算成分钟。
    返回空列表时组件会退回内置的成都示例点位，片头照常播完。
    """
    pois: list[dict[str, Any]] = []
    seen: set[str] = set()
    for day in (result.get("itinerary") or {}).get("days") or []:
        for item in day.get("items") or []:
            if item.get("type") != "attraction":
                continue
            name = str(item.get("name") or "").strip()
            lng, lat = item.get("lng"), item.get("lat")
            if not name or name in seen:
                continue
            if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
                continue
            if not (73 <= float(lng) <= 136 and 3 <= float(lat) <= 54):
                continue
            start = _hhmm_to_min(item.get("start"))
            end = _hhmm_to_min(item.get("end"))
            seen.add(name)
            pois.append(
                {
                    "name": name,
                    "lng": round(float(lng), 6),
                    "lat": round(float(lat), 6),
                    "day": int(day.get("day") or 0),
                    "start": str(item.get("start") or ""),
                    "end": str(item.get("end") or ""),
                    "stay": max(0, end - start) if start is not None and end is not None else 0,
                    "note": str(item.get("note") or ""),
                    "cost": item.get("cost") or 0,
                    "img": str(item.get("pic") or item.get("photo") or ""),
                    # 高德图库里该地点自己的实拍图，最多 4 张；没有就留空让组件占位
                    "photos": [str(u) for u in (item.get("photos") or []) if u][:4],
                }
            )
    return pois[:8]

def _play_intro(result: dict[str, Any]) -> dict[str, Any] | None:
    """全屏电影级开场。

    组件在「播完」或用户点「跳过片头」时回传 componentValue，Streamlit 因此 rerun 一次；
    这一轮 _intro_pending 已经是 False，脚本继续往下走 show_result()，即进入行程详情。
    首帧返回 None，所以本轮直接 st.stop()，让片头独占一屏。
    """
    c = result.get("constraints") or {}
    # 片头这一轮只渲染 iframe，顺手把主区留白收掉，避免上下各留一条黑边
    st.markdown(
        """
        <style>
          .block-container { padding-top: 0.6rem !important; padding-bottom: 0 !important; max-width: 100% !important; }
          iframe[title*="map_intro"] { width: 100% !important; border: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    previous = st.session_state.get("_intro_result")
    # 真实行程 -> 片头点位：没有可用的景点（规划失败 / 缺高德 Key）时传空列表，
    # 组件会立刻退回内置示例，不会白屏。
    pois = _intro_pois(result)
    event = map_intro(
        key="plan_intro",
        payload={
            "origin": str(st.session_state.get("f_origin") or ORIGIN_DEFAULT),
            "city": str(c.get("city") or ""),
            "days": int(c.get("days") or 0),
            "people": int(c.get("people") or 0),
            "pois": pois,
        },
    )
    if event and event != previous:
        st.session_state["_intro_result"] = event
    return event


def main() -> None:
    setup_detail_log()
    apply_pending_constraints()
    apply_pending_preset()
    apply_pending_chat()

    # 后台规划任务进行中：进度卡独占一屏。主线程不被 run() 占住，
    # 「取消规划」点一下就是一次正常 rerun，用户不用等后台请求跑完。
    job = st.session_state.get("_agent_job")
    if job is not None:
        # 两段式：大纲一出来就先播片头，再用大纲页盖住细节补全的等待
        if job.outline is not None and not st.session_state.get("_intro_played"):
            st.session_state["_intro_played"] = True
            inject_css()
            _play_intro(job.outline)
            st.stop()
        if job.outline is not None:
            _render_outline_job(job)
            st.stop()
        _render_plan_job(job)
        st.stop()

    # 全局 Header 已下线：品牌名收进空状态页，避免顶部黑边与强制标题
    missing = config.require_keys()
    if missing:
        st.warning(
            "缺少密钥：" + "、".join(zh_labels.key_label(name) for name in missing) + "。"
            "本地运行请把 .env.example 复制为 .env 并填写对应变量后重启；"
            "线上部署请打开右上角「设置 → 密钥」，按示例格式粘贴后点保存。"
        )

    # 输入卡渲染在脚本末尾，这一轮先用 session_state 里的条件
    payload = _payload_from_state()

    # 结果页「调整条件」提交后，用同一套 payload 流程重算
    rerun_request = st.session_state.pop("_rerun_request", None)
    # 卡片「⚡ 一键生成这个行程」和底部「开始规划」都只写标记，这里统一开跑一次
    start_run = (
        st.session_state.pop("_run_now", False)
        or st.session_state.pop("_run_preset", False)
        or rerun_request is not None
    )
    if start_run:
        st.session_state.pop("result", None)
        # 开跑了就别让详情弹窗压在进度上
        st.session_state["_open_detail"] = ""
        if _start_plan(str(rerun_request) if rerun_request is not None else payload["request"], payload):
            st.rerun()

    plan_error = st.session_state.pop("_plan_error", None)
    if plan_error:
        st.error(plan_error)

    result = st.session_state.get("result")
    css_done = False
    if not result:
        render_empty_state()
        # 首页的样式由 render_empty_state() 在预载哨兵之后注入，这里不再重复
        css_done = True
    else:
        if st.session_state.pop("_intro_pending", False):
            # 片头独占一屏：先播，回传后再 rerun 走到下面的 show_result
            # 片头这一屏也要先注入全局样式：否则上一轮（规划页）的 <style>
            # 会被当成过期节点清掉，进度卡到片头之间会闪出一段无样式白字
            inject_css()
            _play_intro(result)
            st.stop()
        show_result(result)

    # 输入卡只属于首页：结果详情页 / 卡片详情弹窗都不渲染它（不是 display:none）。
    detail_open = bool(str(st.session_state.get("_open_detail") or ""))
    if not result and not detail_open:
        render_composer()
        # 输入卡高度会随聊天记录变化，主区底部留白跟着它走
        _embed_layout_sync()

    # 详情弹窗最后打开：它压在整页之上，前面该回填的条件这轮都已经落好。
    render_open_detail()

    # 背景必须在输入卡之后注入：换省份时旧城市会被 _sync_select 重置，
    # 早于控件读 session_state 就会拿到上一轮的失效城市，背景慢一整拍。
    if not css_done:
        inject_css()


if __name__ == "__main__":
    main()
