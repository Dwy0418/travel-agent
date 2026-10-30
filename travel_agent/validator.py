"""校验与自纠错层：6 项纯 Python 校验，问题以自然语言返回给模型重排。

校验项：天气 / 路程耗时 / 预算 / 营业时间 / 同行人约束 / 重复景点
（所有数字由代码计算，模型不参与算数）
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

from . import config, tools

Router = Callable[[str, dict[str, Any]], dict[str, Any]]

INDOOR_KEYWORDS = ("博物馆", "美术馆", "展览馆", "纪念馆", "图书馆", "科技馆", "寺", "庙", "宫", "室内", "温泉", "商场", "影院")
CLOSED_WORDS = ("闭馆", "休息", "不开放", "暂停开放", "停止营业")
WEEKDAY_INDEX = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6, "周天": 6}
ELDER_WORDS = ("老人", "长辈", "父母", "爷爷", "奶奶", "外公", "外婆", "腿脚")
CHILD_WORDS = ("小孩", "儿童", "孩子", "亲子", "宝宝", "婴儿")


def default_router(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return tools.execute_tool(name, args)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def parse_hm(text: Any) -> int | None:
    """'09:30' / '9：30' -> 分钟数。"""
    if not isinstance(text, str):
        return None
    match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def fmt_hm(minutes: float) -> str:
    total = int(round(minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


def to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
    return None


def weekday_cn(value: date) -> str:
    return ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][value.weekday()]


def is_indoor(item: dict[str, Any]) -> bool:
    if item.get("indoor") is True:
        return True
    if item.get("indoor") is False:
        return False
    name = f"{item.get('name', '')}{item.get('note', '')}"
    return any(word in name for word in INDOOR_KEYWORDS)


TIME_RANGE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})")


def opening_windows(text: str) -> list[tuple[int, int]]:
    """解析营业时间，支持分时段（如「10:00-12:00 13:00-20:00」午休闭馆）。"""
    if not text:
        return []
    if any(word in text for word in ("全天", "24小时", "24 小时")):
        return [(0, 24 * 60)]
    windows: list[tuple[int, int]] = []
    for hour1, minute1, hour2, minute2 in TIME_RANGE.findall(text):
        start = int(hour1) * 60 + int(minute1)
        end = int(hour2) * 60 + int(minute2)
        if end <= start:  # 跨夜营业（如 20:00-02:00）按到次日处理
            end += 24 * 60
        windows.append((start, end))
    return windows


def opening_window(text: str) -> tuple[int, int] | None:
    """兼容旧接口：返回最早的一段营业窗口。"""
    windows = opening_windows(text)
    return windows[0] if windows else None


def closed_weekdays(text: str) -> list[int]:
    if not text or not any(word in text for word in CLOSED_WORDS):
        return []
    if re.search(r"周一\s*[至到\-~]\s*周日", text) and not any(
        word in text for word in CLOSED_WORDS[:3]
    ):
        return []
    days: list[int] = []
    for label, index in WEEKDAY_INDEX.items():
        if label in text and index not in days:
            days.append(index)
    return days


def day_date(day: dict[str, Any], constraints: dict[str, Any], index: int) -> date | None:
    parsed = to_date(day.get("date"))
    if parsed:
        return parsed
    start = to_date(constraints.get("start_date")) or date.today()
    return start + timedelta(days=index)


def weather_query_days(constraints: dict[str, Any], day_count: int | None = None) -> int:
    """计算覆盖整个行程跨度需要查询的天数（预取与校验必须用同一算法，才能命中同一份缓存）。"""
    start = to_date(constraints.get("start_date")) or date.today()
    span = (start - date.today()).days + max(1, day_count or int(constraints.get("days") or 1))
    return max(1, min(span + 1, 16))


def weather_map(
    itinerary: dict[str, Any], constraints: dict[str, Any], router: Router
) -> dict[str, dict[str, Any]]:
    """一次查询覆盖整个行程跨度，按日期索引；查询失败返回空字典（跳过天气校验）。"""
    lng, lat = constraints.get("lng"), constraints.get("lat")
    if lng is None or lat is None:
        return {}
    days = weather_query_days(constraints, len(itinerary.get("days") or []))
    result = router("get_weather", {"lng": lng, "lat": lat, "days": days})
    if not result.get("success"):
        return {}
    return {item["date"]: item for item in result.get("days") or [] if item.get("date")}


# --------------------------------------------------------------------------- #
# 主校验入口
# --------------------------------------------------------------------------- #
def validate(
    itinerary: dict[str, Any],
    constraints: dict[str, Any],
    router: Router | None = None,
    extra_transport: float = 0.0,
) -> list[str]:
    """返回自然语言问题列表；空列表表示行程通过全部校验。

    extra_transport：点间交通费（由 Agent 按 plan_route 结果累加后传入），
    模型通常只在景点的 transport 字段写方式、不单独生成交通条目，这里补上避免预算漏算。
    """
    router = router or default_router
    issues: list[str] = []
    days = itinerary.get("days") or []
    expect = int(constraints.get("days") or 0)
    if not days:
        return ["行程没有任何内容，请按天输出结构化行程"]
    if expect and len(days) != expect:
        issues.append(f"行程共 {len(days)} 天，与用户要求的 {expect} 天不一致，请补齐或裁剪")

    weather = weather_map(itinerary, constraints, router)
    companion_text = " ".join(constraints.get("companions") or [])
    elder = any(word in companion_text for word in ELDER_WORDS)
    child = any(word in companion_text for word in CHILD_WORDS)
    seen: dict[str, list[str]] = {}

    for index, day in enumerate(days):
        day_no = day.get("day") or index + 1
        label = f"第{day_no}天"
        current = day_date(day, constraints, index)
        day_weather = weather.get(current.isoformat()) if current else None
        items = day.get("items") or []
        attractions = [item for item in items if item.get("type") == "attraction"]
        if not attractions:
            issues.append(f"{label}没有任何景点安排，请补充")

        # 时间格式与先后顺序
        prev_end: int | None = None
        for item in items:
            name = item.get("name") or "未命名项"
            start, end = parse_hm(item.get("start")), parse_hm(item.get("end"))
            if start is None or end is None:
                issues.append(f"{label}「{name}」的时间不是「时:分」格式，请修正")
                continue
            if end <= start:
                issues.append(
                    f"{label}「{name}」结束时间 {item.get('end')} 不晚于开始时间 {item.get('start')}，请修正"
                )
            if prev_end is not None and start < prev_end:
                issues.append(
                    f"{label}「{name}」与上一项时间重叠（上一项 {fmt_hm(prev_end)} 才结束），请调整顺序"
                )
            prev_end = max(prev_end or 0, end)

        # 坐标完整性（禁止编造地点）
        for item in attractions:
            if item.get("lng") is None or item.get("lat") is None:
                issues.append(
                    f"{label}「{item.get('name')}」缺少经纬度，请用「地点检索」返回的真实坐标补全，禁止编造"
                )

        # 天气校验
        if day_weather and not day_weather.get("is_outdoor_ok", True):
            rain = day_weather.get("rain_prob")
            for item in [i for i in attractions if not is_indoor(i)]:
                issues.append(
                    f"{label}（{current}）安排了户外景点「{item.get('name')}」，但当天降水概率 "
                    f"{rain}%，户外景点不适宜，请换到晴天或改成室内景点"
                    f"（室内候选不够时，先用「地点检索」搜「博物馆」「美术馆」「展览馆」「科技馆」"
                    f"「商场」等关键词补充；不要靠重复同一两个室内景点凑数）"
                )

        # 路程耗时校验
        dwell = 0
        for item in attractions:
            start, end = parse_hm(item.get("start")), parse_hm(item.get("end"))
            if start is not None and end is not None and end > start:
                dwell += end - start
        transport_min = 0.0
        legs: list[tuple[str, str, int]] = []
        for prev_item, cur_item in zip(attractions, attractions[1:]):
            if any(cur_item.get(k) is None for k in ("lng", "lat")) or any(
                prev_item.get(k) is None for k in ("lng", "lat")
            ):
                continue
            mode = cur_item.get("transport")
            mode = mode if mode in ("driving", "walking", "transit") else "driving"
            route = router(
                "plan_route",
                {
                    "origin_lng": prev_item["lng"],
                    "origin_lat": prev_item["lat"],
                    "dest_lng": cur_item["lng"],
                    "dest_lat": cur_item["lat"],
                    "mode": mode,
                },
            )
            seconds = route.get("duration_sec")
            if seconds is None:
                continue
            minutes = float(seconds) / 60
            transport_min += minutes
            legs.append((prev_item.get("name") or "?", cur_item.get("name") or "?", round(minutes)))
        total_min = dwell + transport_min + config.LUNCH_MINUTES
        dwell_cap = (4 if elder else 5) * 60  # 提示词里的软上限，只用于给配额建议，不作硬约束
        if attractions and total_min > config.DAILY_HOURS * 60:
            worst = max(legs, key=lambda leg: leg[2]) if legs else None
            detail = ""
            if worst:
                detail = f"其中最远一段「{worst[0]}→{worst[1]}」车程约 {worst[2]} 分钟；"
            gap = total_min - config.DAILY_HOURS * 60
            allowed_dwell = config.DAILY_HOURS * 60 - transport_min - config.LUNCH_MINUTES
            # 与提示词的「游览 ≤5h（带老人 ≤4h）」取更严者，并留 10 分钟缓冲，
            # 否则模型会贴着 8 小时上限反复微调（6.50h→6.17h 仍差 4 分钟）。
            dwell_cap = (4 if elder else 5) * 60
            target_dwell = max(0.0, min(allowed_dwell, dwell_cap) - 10)
            if worst:
                per_item = target_dwell / max(1, len(attractions))
                advice = (
                    f"请把游览总时长从当前 {dwell / 60:.2f} 小时（{dwell:.0f} 分钟）压到 "
                    f"≤ {target_dwell / 60:.2f} 小时（{target_dwell:.0f} 分钟）以内"
                    f"（交通 + 午休已占 {transport_min / 60 + config.LUNCH_MINUTES / 60:.2f} 小时，"
                    f"游览上限 {dwell_cap / 60:.0f} 小时）；"
                    f"即 {len(attractions)} 个景点平均每个不超过 {per_item:.0f} 分钟——"
                    f"只调顺序或换交通方式无效，必须真的改小每个景点的起止时间跨度"
                )
                if gap >= 30:
                    advice += f"；若压缩后仍排不下，就删掉最远的「{worst[1]}」，当天只保留 2 个景点"
            else:
                advice = "请减少景点"
            issues.append(
                f"{label}总耗时约 {total_min / 60:.2f} 小时（游览 {dwell / 60:.2f} 小时 + 交通 "
                f"{transport_min / 60:.2f} 小时 + 午休 1 小时），超过当日可用 {config.DAILY_HOURS:.0f} 小时；"
                f"{detail}超出 {gap:.0f} 分钟，{advice}"
            )

        # 营业时间校验
        for item in attractions:
            name = item.get("name") or ""
            text = f"{name} {item.get('open_time') or ''} {item.get('note') or ''}"
            closed = closed_weekdays(text)
            if closed and current and current.weekday() in closed:
                issues.append(
                    f"{label}（{current} {weekday_cn(current)}）安排了「{name}」，但该景点 "
                    f"{weekday_cn(current)} 闭馆/休息，请调整时间"
                )
                continue
            windows = opening_windows(item.get("open_time") or "")
            start = parse_hm(item.get("start"))
            if windows and start is not None:
                # 落在任一分时段内、且距该时段结束还有 30 分钟以上，才算能进馆
                ok = any(open_at <= start <= close_at - 30 for open_at, close_at in windows)
                if not ok:
                    issues.append(
                        f"{label}「{name}」计划 {item.get('start')} 游览，但营业时间为 "
                        f"{item.get('open_time')}，不在开放时段内，请调整"
                    )

        # 同行人约束校验
        if elder or child:
            who = "老人" if elder else "小孩"
            if len(attractions) > config.MAX_ATTRACTIONS_PER_DAY:
                issues.append(
                    f"{label}安排了 {len(attractions)} 个景点，同行有{who}，单日不要超过 "
                    f"{config.MAX_ATTRACTIONS_PER_DAY} 个，请减少景点"
                )
            walk = sum(
                item.get("walk_km")
                if item.get("walk_km") is not None
                else config.WALK_KM_PER_ATTRACTION
                for item in attractions
            )
            if walk > config.ELDER_WALK_KM_LIMIT:
                issues.append(
                    f"{label}步行距离约 {walk:.1f} 公里，同行有{who}，单日步行不要超过 "
                    f"{config.ELDER_WALK_KM_LIMIT:.0f} 公里。注意「园内步行距离」指景区内部的步行距离"
                    f"（单个景点通常 0.5-2 公里），不是景点之间的路程（景点间路程属于「交通方式」）；"
                    f"请把各景点的园内步行距离改成实际值，并去掉步行量大的景点"
                )

        for item in attractions:
            seen.setdefault(item.get("name") or "", []).append(label)

    # 重复景点校验
    for name, labels in seen.items():
        if name and len(labels) > 1:
            issues.append(f"「{name}」在行程中出现 {len(labels)} 次（{'、'.join(labels)}），请删除重复项")

    # 预算校验（纯代码计算）
    budget_result = tools.estimate_budget(
        itinerary,
        days=int(constraints.get("days") or len(days)),
        people=int(constraints.get("people") or 1),
        rooms=constraints.get("rooms"),
        food_per_person_per_day=constraints.get("food_per_person_per_day"),
        hotel_per_night=constraints.get("hotel_per_night"),
    )
    budget = constraints.get("budget")
    if budget and budget_result.get("success"):
        total = budget_result["total"] + max(0.0, extra_transport)
        if total > float(budget):
            parts = budget_result["breakdown"]
            transport = parts["transport"] + max(0.0, extra_transport)
            issues.append(
                f"当前行程总预算 {total:.0f} 元（门票 {parts['ticket']:.0f} + 交通 "
                f"{transport:.0f} + 餐饮 {parts['food']:.0f} + 住宿 {parts['hotel']:.0f}），"
                f"超过用户设定的 {float(budget):.0f} 元预算，请减少收费景点或调整交通/住宿标准"
            )
    return issues
