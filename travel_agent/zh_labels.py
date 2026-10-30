"""显示层中英对照表（唯一来源）。

界面、执行日志、导出的 Markdown 里出现的英文枚举值一律从这里取中文名；
渲染代码里禁止再写 if/else 或字符串 replace 来翻译。

这里只做「怎么显示」，不参与任何规划/校验/工具调用逻辑。
"""
from __future__ import annotations

import re
from typing import Any

#: 页面标题（浏览器标签页）
PAGE_TITLE = "国内旅行规划智能体"
#: 页面主标题
APP_TITLE = "🧭 国内旅行规划智能体"
#: 副标题（只保留品牌名，一句话说完）
APP_CAPTION = "高德地图 · Open-Meteo 天气 · 规则自校验"

#: 行程条目类型
ITEM_TYPE_LABELS: dict[str, str] = {
    "attraction": "景点",
    "rest": "用餐休息",
    "transport": "交通接驳",
    "hotel": "住宿",
    "meal": "用餐",
    "food": "用餐",
    "shopping": "购物",
    "other": "其他",
}

#: 条目交通方式
TRANSPORT_LABELS: dict[str, str] = {
    "driving": "驾车",
    "walking": "步行",
    "transit": "公交",
    "riding": "骑行",
    "taxi": "打车",
    "none": "无需移动",
    "": "无需移动",
}

#: 预算明细项目
BUDGET_LABELS: dict[str, str] = {
    "ticket": "门票",
    "transport": "交通",
    "food": "餐饮",
    "hotel": "住宿",
    "other": "其他",
}

#: 规划状态
STATUS_LABELS: dict[str, str] = {
    "ok": "通过",
    "max_iterations": "已达迭代上限",
    "failed": "规划失败",
}

#: 缺少密钥时页面显示的中文名（环境变量名只在配置文件里出现）
KEY_LABELS: dict[str, str] = {
    "DEEPSEEK_API_KEY": "DeepSeek 接口密钥",
    "AMAP_KEY": "高德地图密钥",
}


def key_label(value: Any) -> str:
    return KEY_LABELS.get(str(value or "").strip().upper(), "接口密钥")


#: 工具中文名（技术明细里用）
TOOL_LABELS: dict[str, str] = {
    "geocode": "地名解析",
    "search_poi": "地点检索",
    "get_weather": "天气查询",
    "plan_route": "路线规划",
    "estimate_budget": "预算核算",
}

#: 接口失败原因 -> 友好中文（页面只给结论，原始原因进后台日志）
ERROR_RULES: tuple[tuple[str, str], ...] = (
    ("INVALID_USER_KEY", "高德接口暂时不可用：密钥无效，已跳过该点"),
    ("USERKEY_PLAT_NOMATCH", "高德接口暂时不可用：密钥与平台不匹配，已跳过该点"),
    ("INSUFFICIENT_PRIVILEGES", "高德接口暂时不可用：密钥没有该服务权限，已跳过该点"),
    ("DAILY_QUERY_OVER_LIMIT", "高德接口今日调用量已达上限，已跳过该点"),
    ("SERVICE_NOT_AVAILABLE", "高德接口暂时不可用，已跳过该点"),
    ("ABROAD", "高德接口暂不支持该区域，已跳过该点"),
    ("timeout", "接口响应超时，已跳过该点"),
    ("timed out", "接口响应超时，已跳过该点"),
    ("超时", "接口响应超时，已跳过该点"),
    ("connection", "网络连接失败，已跳过该点"),
    ("网络", "网络连接失败，已跳过该点"),
)


def _clean(value: Any) -> str:
    return str(value or "").strip().lower()


def item_type(value: Any) -> str:
    """行程表「类型」列。未知值统一显示为「其他」。"""
    return ITEM_TYPE_LABELS.get(_clean(value), "其他")


def transport(value: Any) -> str:
    """行程表「交通」列。空值 / none 显示「无需移动」。"""
    return TRANSPORT_LABELS.get(_clean(value), "无需移动")


def route_mode(value: Any) -> str:
    """路线规划的方式；缺省按驾车处理（与工具层默认值一致）。"""
    return TRANSPORT_LABELS.get(_clean(value) or "driving", "驾车")


def budget_item(value: Any) -> str:
    """预算明细「项目」列。"""
    return BUDGET_LABELS.get(_clean(value), "其他")


def status(value: Any) -> str:
    """规划状态。"""
    return STATUS_LABELS.get(_clean(value), "未知状态")


def tool_name(value: Any) -> str:
    return TOOL_LABELS.get(_clean(value), "工具")


#: 校验问题在「执行过程」里的显示长度（完整文本仍会回灌给模型、仍进未解决列表）
ISSUE_TEXT_LIMIT = 56


def short_issue(text: Any, limit: int = ISSUE_TEXT_LIMIT) -> str:
    """校验问题只显示结论：去掉结尾那段长长的操作建议，过长再按标点截断。

    例如「第1天安排了户外景点「雁南飞茶田」，但当天降水概率 70%，
    户外景点不适宜，请换到晴天或改成室内景点（室内候选不够时，先用…补数）」
    显示为「第1天安排了户外景点「雁南飞茶田」，但当天降水概率 70%，户外景点不适宜……」。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    trimmed = re.sub(r"[（(][^（）()]{12,}[）)]\s*$", "", raw).strip()
    if len(trimmed) > limit:
        cut = max(trimmed.rfind(mark, 0, limit) for mark in "，；、,;")
        trimmed = trimmed[:cut] if cut >= limit // 2 else trimmed[:limit].rstrip("，、；,;：: ")
        trimmed += "……"
    return trimmed or raw


def round_label(round_no: Any) -> str:
    """日志前缀：【准备】/【第 1 轮】（中文方括号，括号内不留多余空格）。"""
    try:
        number = int(round_no or 0)
    except (TypeError, ValueError):
        number = 0
    return f"【第 {number} 轮】" if number else "【准备】"


#: 默认展示的关键节点（收到的需求 / 每轮决策 / 校验 / 重排 / 完成）
KEY_TRACE_KINDS = frozenset(
    {
        "constraint",
        "prepare",
        "decision",
        "validation",
        "issue",
        "give_up",
        "budget",
        "cache",
        "parse_error",
        "truncated",
        "done",
    }
)
#: 收进折叠区的技术明细（每次工具调用的参数、耗时）
TECH_TRACE_KINDS = frozenset({"tool_call", "tool_result"})

#: 关键日志默认最多显示多少行
KEY_TRACE_LIMIT = 30


def is_key_trace(entry: dict[str, Any]) -> bool:
    return entry.get("kind") in KEY_TRACE_KINDS


def is_tech_trace(entry: dict[str, Any]) -> bool:
    return entry.get("kind") in TECH_TRACE_KINDS


# --------------------------------------------------------------------------- #
# 执行日志：工具调用 / 返回改成人话
# --------------------------------------------------------------------------- #
def describe_tool_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    city: str = "",
    origin: str = "",
    dest: str = "",
) -> str:
    """把「调用工具：plan_route(origin_lng=...)」翻成人能读的一句。"""
    args = args or {}
    if name == "geocode":
        return f"查询地名：{args.get('place_name') or '目的地'}"
    if name == "search_poi":
        keyword = args.get("keyword") or "地点"
        city = str(args.get("city") or city or "")
        count = args.get("count")
        head = f"搜索{city}{keyword}" if city else f"搜索{keyword}"
        return f"{head}（取 {count} 个）" if count else head
    if name == "get_weather":
        days = args.get("days")
        return f"查询{city}未来 {days} 天天气" if days else f"查询{city}天气"
    if name == "plan_route":
        return f"规划路线：{origin or '起点'} → {dest or '终点'}（{route_mode(args.get('mode'))}）"
    if name == "estimate_budget":
        return "核算预算（门票 + 交通 + 餐饮 + 住宿）"
    return f"调用{tool_name(name)}"


def describe_tool_result(name: str, result: dict[str, Any] | None = None) -> str:
    """把工具返回压成一句结论，不含任何原始坐标。"""
    result = result or {}
    if not result.get("success"):
        return friendly_error(result.get("error"))
    if name == "geocode":
        city = str(result.get("city") or "")
        name_text = str(result.get("name") or "目的地")
        return f"定位成功：{name_text}" + (f"（{city}）" if city and city != name_text else "")
    if name == "search_poi":
        pois = result.get("pois") or []
        names = [str(poi.get("name")) for poi in pois[:4] if poi.get("name")]
        count = result.get("count") or len(pois)
        return f"搜到 {count} 个地点" + (f"：{'、'.join(names)}" if names else "")
    if name == "get_weather":
        days = result.get("days") or []
        rainy = sum(1 for day in days if day.get("is_rainy"))
        return f"未来 {len(days)} 天天气已获取" + (f"，其中 {rainy} 天有雨" if rainy else "")
    if name == "plan_route":
        km = float(result.get("distance_m") or 0) / 1000
        minutes = round(float(result.get("duration_sec") or 0) / 60)
        cost = float(result.get("cost_yuan") or 0)
        text = f"路线结果：{km:.1f} 公里 · {minutes} 分钟 · {cost:.0f} 元"
        if result.get("estimated"):
            text += "（接口未返回，已按直线估算）"
        return text
    if name == "estimate_budget":
        return f"预算合计 {float(result.get('total') or 0):.0f} 元"
    return "结果已返回"


def friendly_error(text: Any) -> str:
    """异常提示统一成友好中文；已经是中文的（例如缺少密钥）原样保留。"""
    raw = str(text or "").strip()
    lowered = raw.lower()
    for needle, message in ERROR_RULES:
        if needle.lower() in lowered:
            return message
    if raw and not raw.isascii():
        return raw
    return "接口暂时不可用，已跳过该点"


# --------------------------------------------------------------------------- #
# 折叠区「技术明细」：保留工作量，但不出现坐标与英文参数名
# --------------------------------------------------------------------------- #
def _call_params(name: str, args: dict[str, Any]) -> str:
    if name == "geocode":
        return f"地名：{args.get('place_name') or '—'}"
    if name == "search_poi":
        return (
            f"城市：{args.get('city') or '—'}｜关键词：{args.get('keyword') or '—'}"
            f"｜数量：{args.get('count') or '—'}"
        )
    if name == "get_weather":
        return f"天数：{args.get('days') or '—'}｜坐标已记录到后台日志"
    if name == "plan_route":
        return f"方式：{route_mode(args.get('mode'))}｜起终点坐标已记录到后台日志"
    if name == "estimate_budget":
        return "按当前行程草稿核算"
    return "参数已记录到后台日志"


def describe_technical(entry: dict[str, Any]) -> str:
    """一条技术明细（工具名 / 参数摘要 / 耗时 / 缓存），供折叠区展示。"""
    kind = entry.get("kind")
    tool = entry.get("tool")
    parts: list[str] = [tool_name(tool) if tool else "工具"]
    if kind == "tool_call":
        parts.append(_call_params(str(tool or ""), entry.get("args") or {}))
    elif kind == "tool_result":
        parts.append("返回成功" if entry.get("args_ok", True) else "返回失败")
    if entry.get("elapsed_ms") is not None:
        parts.append(f"耗时 {entry['elapsed_ms']} 毫秒")
    if entry.get("cache"):
        parts.append("命中缓存")
    return "｜".join(str(part) for part in parts if part)


# --------------------------------------------------------------------------- #
# 表格行
# --------------------------------------------------------------------------- #
def plan_rows(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    """行程表：类型 / 交通两列直接出中文。"""
    rows: list[dict[str, Any]] = []
    for index, day in enumerate(itinerary.get("days") or []):
        for item in day.get("items") or []:
            rows.append(
                {
                    "天": day.get("day") or index + 1,
                    "日期": day.get("date", ""),
                    "时间": f"{item.get('start', '')}-{item.get('end', '')}",
                    "类型": item_type(item.get("type")),
                    "安排": item.get("name", ""),
                    "交通": transport(item.get("transport")),
                    "花费（元）": item.get("cost", 0),
                    "营业时间": item.get("open_time", ""),
                    "备注": item.get("note", ""),
                }
            )
    return rows


def budget_rows(breakdown: dict[str, Any] | None) -> list[dict[str, Any]]:
    """预算明细：项目列直接出中文。"""
    return [{"项目": budget_item(key), "金额（元）": value} for key, value in (breakdown or {}).items()]

