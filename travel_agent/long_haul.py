"""跨城大交通：去程 / 回程条目的确定性生成。

高德只给驾车/公交路线，给不了机票和高铁票价。这里不伪装成实时票价：
按公开的常见区间估算，并在条目名称和备注里明确写「估算 / 非实时查询」。
生成的条目写入 itinerary.days[].items，和普通条目走同一个 summarize_cost。
"""
from __future__ import annotations

import math
from typing import Any

MODE_LABELS = {
    "high_speed": "高铁/动车",
    "flight": "飞机",
    "train": "普速火车",
    "coach": "长途汽车",
    "self_drive": "自驾",
    "auto": "不限/由 AI 决定",
}

DEFAULT_MODE = "high_speed"


def mode_label(mode: str | None) -> str:
    return MODE_LABELS.get(str(mode or DEFAULT_MODE), MODE_LABELS[DEFAULT_MODE])


def _round5(value: float) -> float:
    return float(int(round(value / 5.0) * 5))


def estimate(mode: str, distance_km: float, people: int = 1) -> dict[str, Any]:
    """按里程区间估算单程票价与耗时；返回值里 estimated 永远为 True。"""
    distance = max(1.0, float(distance_km or 800.0))
    people = max(1, int(people or 1))
    if mode == "flight":
        unit = min(2200.0, max(450.0, 400.0 + 0.9 * distance))
        minutes = min(360, int(120 + distance / 700 * 60))
        note = f"机票按公开淡旺季区间估算（约 ¥{_round5(unit):.0f}/人），非实时查询；含机场往返约 {minutes / 60:.1f} 小时。"
    elif mode == "train":
        unit = max(30.0, 0.16 * distance)
        minutes = min(1200, int(30 + distance / 90 * 60))
        note = f"普速火车按里程区间估算（约 ¥{_round5(unit):.0f}/人），非实时查询；单程约 {minutes / 60:.1f} 小时。"
    elif mode == "coach":
        unit = max(25.0, 0.32 * distance)
        minutes = min(900, int(20 + distance / 70 * 60))
        note = f"长途汽车按里程区间估算（约 ¥{_round5(unit):.0f}/人），非实时查询；单程约 {minutes / 60:.1f} 小时。"
    elif mode == "self_drive":
        unit = max(40.0, 0.8 * distance)
        minutes = min(1440, int(30 + distance / 80 * 60))
        note = f"油费+过路费按里程估算（约 ¥{_round5(unit):.0f}/车），非实时查询；单程约 {minutes / 60:.1f} 小时。"
    else:  # high_speed
        unit = max(60.0, 0.45 * distance)
        minutes = min(600, int(30 + distance / 250 * 60))
        note = f"高铁二等座按里程区间估算（约 ¥{_round5(unit):.0f}/人），非实时查询；单程约 {minutes / 60:.1f} 小时。"
    unit = _round5(unit)
    total = unit if mode == "self_drive" else _round5(unit * people)
    return {
        "mode": mode,
        "label": mode_label(mode),
        "distance_km": round(distance, 1),
        "unit_price": unit,
        "total": total,
        "duration_min": minutes,
        "note": note,
        "estimated": True,
    }


def resolve_mode(mode: str | None, distance_km: float) -> str:
    """「不限/由 AI 决定」按距离选：近途汽车、中途高铁、远途飞机。"""
    code = str(mode or DEFAULT_MODE)
    if code in ("high_speed", "flight", "train", "coach", "self_drive"):
        return code
    if distance_km < 250:
        return "coach"
    if distance_km < 1200:
        return "high_speed"
    return "flight"


def haversine_km(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if not a or not b:
        return None
    lng1, lat1 = a
    lng2, lat2 = b
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * radius * math.asin(math.sqrt(h)), 1)


def _minutes(text: Any) -> int | None:
    raw = str(text or "").strip()
    if ":" not in raw:
        return None
    try:
        hour, minute = raw.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def _clock(minutes: int) -> str:
    minutes = max(0, minutes)
    if minutes >= 24 * 60:
        minutes -= 24 * 60
        return f"次日 {minutes // 60:02d}:{minutes % 60:02d}"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _item(kind: str, origin: str, dest: str, start_min: int, duration: int, cost: float,
          mode: str, note: str, coord: tuple[float, float] | None) -> dict[str, Any]:
    label = mode_label(mode)
    name = ("去程" if kind == "outbound" else "返程") + f"：{origin}→{dest}（{label} · 估算）"
    return {
        "start": _clock(start_min),
        "end": _clock(start_min + duration),
        "type": "transport",
        "name": name,
        "lng": coord[0] if coord else None,
        "lat": coord[1] if coord else None,
        "cost": cost,
        "indoor": False,
        "open_time": "",
        "transport": "driving" if mode == "self_drive" else "transit",
        "walk_km": 0.0,
        "note": note,
        "long_haul": kind,
    }


def _is_long_haul(item: dict[str, Any]) -> bool:
    name = str(item.get("name") or "")
    return bool(item.get("long_haul")) or name.startswith(("去程：", "返程：", "回程："))


def _sort_key(item: dict[str, Any]) -> tuple[int, int]:
    moment = _minutes(item.get("start"))
    return (moment if moment is not None else 24 * 60, 0)


def _trim(items: list[dict[str, Any]], min_start: int | None, max_end: int | None,
          max_attractions: int, max_attr_minutes: int) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    attractions = 0
    attr_minutes = 0
    for item in sorted(items, key=_sort_key):
        if _is_long_haul(item):
            continue
        start = _minutes(item.get("start"))
        end = _minutes(item.get("end"))
        if min_start is not None and start is not None and start < min_start:
            continue
        if max_end is not None and end is not None and end > max_end:
            continue
        if str(item.get("type") or "") == "attraction":
            if attractions >= max_attractions:
                continue
            span = (end - start) if start is not None and end is not None and end > start else 120
            if attr_minutes + span > max_attr_minutes:
                continue
            attractions += 1
            attr_minutes += span
        kept.append(item)
    return kept


def apply(itinerary: dict[str, Any], constraints: dict[str, Any], distance_km: float | None,
          origin_coord: tuple[float, float] | None = None,
          dest_coord: tuple[float, float] | None = None) -> dict[str, Any]:
    """把去程/回程写进首末日，并裁掉首末日排满的景点。返回估算说明。"""
    days = itinerary.get("days") or []
    if not days:
        return {}
    distance = float(distance_km or 800.0)
    mode = resolve_mode(constraints.get("transport_mode"), distance)
    people = max(1, int(constraints.get("people") or 1))
    one_way = estimate(mode, distance, people)
    origin = str(constraints.get("origin") or "出发地")
    dest = str(constraints.get("city") or itinerary.get("city") or "目的地")

    outbound = _item("outbound", origin, dest, 8 * 60, one_way["duration_min"],
                     one_way["total"], mode, one_way["note"], dest_coord)
    inbound = _item("inbound", dest, origin, 14 * 60, one_way["duration_min"],
                    one_way["total"], mode, one_way["note"], origin_coord)

    arrival = 8 * 60 + one_way["duration_min"]
    if len(days) == 1:
        days[0]["items"] = _trim(
            list(days[0].get("items") or []), arrival + 30, 14 * 60 - 60, 1, 120
        ) + [outbound, inbound]
    else:
        days[0]["items"] = [outbound] + _trim(
            list(days[0].get("items") or []), arrival + 30, None, 2, 180
        )
        days[-1]["items"] = _trim(
            list(days[-1].get("items") or []), None, 14 * 60 - 60, 2, 180
        ) + [inbound]
    for day in days:
        day["items"] = sorted(day.get("items") or [], key=_sort_key)

    info = {
        "mode": mode,
        "label": mode_label(mode),
        "distance_km": round(distance, 1),
        "estimated": True,
        "unit_price": one_way["unit_price"],
        "outbound": one_way["total"],
        "inbound": one_way["total"],
        "total": round(one_way["total"] * 2, 1),
        "note": one_way["note"],
    }
    itinerary["long_haul"] = info
    return info
