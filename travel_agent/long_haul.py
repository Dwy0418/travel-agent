"""跨城大交通：去程 / 回程条目的确定性生成。

票价策略：先用高德 V3 公交路径规划（extensions=all）拿铁路真实票价，
拿不到（无 G/D、无对应仓位、网络失败）时回落到分段公布价估算。
两种情况在条目名称、备注和页面说明里都明确区分，不把估算冒充实时。
生成的条目写入 itinerary.days[].items，和普通条目走同一个 summarize_cost。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from . import config, tools

# 真实公布价校准锚点（元/人，单程）。高铁取二等座，普速取硬座，飞机取经济舱常见区间。
# 高铁：京津城际 137km 54.5、成渝高铁 308km 154、京沪高铁济南段 406km 184.5、
#       京广高铁郑州段 693km 309、京沪高铁 1318km 553、京广高铁 2298km 862。
# 普速：京津 K 车 120km 21.5、成渝普速 308km 50.5、京沪 T 车 1318km 156.5、
#       京广 2298km 251。
# 长途汽车：按常见城际客运公布价（短途单价高、长途递减）。
FARE_ANCHORS: dict[str, list[tuple[float, float]]] = {
    "high_speed": [(137, 54.5), (308, 154.0), (406, 184.5), (693, 309.0), (1318, 553.0), (2298, 862.0)],
    "flight": [(500, 480.0), (800, 650.0), (1080, 800.0), (1500, 950.0), (2000, 1150.0), (2500, 1350.0), (3500, 1800.0)],
    "train": [(137, 21.5), (308, 50.5), (800, 110.0), (1318, 156.5), (2298, 251.0)],
    "coach": [(137, 40.0), (280, 100.0), (400, 130.0), (690, 200.0), (1000, 280.0)],
}

# 直线距离 -> 实际铁路/公路里程的经验系数（京沪直线 1067km，铁路 1318km）。
ROUTE_FACTOR = {
    "high_speed": 1.20,
    "train": 1.20,
    "coach": 1.15,
    "self_drive": 1.15,
    "flight": 1.05,
}

AMAP_TRANSIT_URL = "https://restapi.amap.com/v3/direction/transit/integrated"
# 高德 spaces.code 是中文仓位名，不是数字编码。按偏好从高到低选座。
SEAT_PREFERENCE = {
    "high_speed": ("火车二等座", "火车无座", "火车一等座", "火车商务座"),
    "train": ("火车硬座", "火车无座", "火车硬卧下铺", "火车硬卧中铺", "火车硬卧上铺", "火车软座"),
}
HIGH_SPEED_PREFIX = ("G", "D", "C")

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


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _interp(points: list[tuple[float, float]], distance: float) -> float:
    """在公布价锚点上做分段线性插值：短途单价高、长途递减。"""
    pts = sorted(points)
    if distance <= pts[0][0]:
        return pts[0][1] * max(0.6, distance / pts[0][0])
    if distance >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        slope = (y1 - y0) / (x1 - x0)
        return y1 + slope * (distance - x1)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= distance <= x1:
            return y0 + (y1 - y0) * (distance - x0) / (x1 - x0)
    return pts[-1][1]


def route_distance(mode: str, straight_km: float | None) -> float:
    base = max(1.0, float(straight_km or 800.0))
    return round(base * ROUTE_FACTOR.get(mode, 1.0), 1)


def estimate(mode: str, distance_km: float, people: int = 1) -> dict[str, Any]:
    """按公布价分档估算单程票价与耗时；estimated 永远为 True。"""
    distance = max(1.0, float(distance_km or 800.0))
    people = max(1, int(people or 1))
    if mode == "flight":
        unit = min(2400.0, _interp(FARE_ANCHORS["flight"], distance))
        minutes = min(360, int(120 + distance / 700 * 60))
        note = f"机票按经济舱常见区间估算（约 ¥{_round5(unit):.0f}/人），估算，非实时查询；含机场往返约 {minutes / 60:.1f} 小时。"
    elif mode == "train":
        unit = max(10.0, _interp(FARE_ANCHORS["train"], distance))
        minutes = min(1200, int(30 + distance / 90 * 60))
        note = f"普速硬座按公布价分段估算（约 ¥{_round5(unit):.0f}/人），估算，非实时查询；单程约 {minutes / 60:.1f} 小时。"
    elif mode == "coach":
        unit = max(15.0, _interp(FARE_ANCHORS["coach"], distance))
        minutes = min(900, int(20 + distance / 70 * 60))
        note = f"长途汽车按城际公布价分段估算（约 ¥{_round5(unit):.0f}/人），估算，非实时查询；单程约 {minutes / 60:.1f} 小时。"
    elif mode == "self_drive":
        unit = max(30.0, 0.55 * distance + 0.45 * distance)
        minutes = min(1440, int(30 + distance / 80 * 60))
        note = (f"自驾按油费约 0.55 元/公里 + 高速费约 0.45 元/公里估算"
                f"（约 ¥{_round5(unit):.0f}/车），估算，非实时查询；单程约 {minutes / 60:.1f} 小时。")
    else:  # high_speed
        unit = max(30.0, _interp(FARE_ANCHORS["high_speed"], distance))
        minutes = min(720, int(20 + distance / 230 * 60))
        note = f"高铁二等座按公布价分段估算（约 ¥{_round5(unit):.0f}/人），估算，非实时查询；单程约 {minutes / 60:.1f} 小时。"
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


# --------------------------------------------------------------------------- #
# 实时票价：高德 V3 公交路径规划 extensions=all 的 railway.spaces
# 实测返回的是单程/人票面价，不含市内接驳与手续费。
# --------------------------------------------------------------------------- #
def _rail_prefix(trip: Any) -> str:
    raw = str(trip or "").strip().upper()
    return raw[:1] if raw else ""


def _seat_price(spaces: Any, mode: str) -> tuple[str, float] | tuple[str, None]:
    if isinstance(spaces, dict):
        spaces = [spaces]
    table: dict[str, float] = {}
    for space in spaces or []:
        if not isinstance(space, dict):
            continue
        code = str(space.get("code") or "").strip()
        cost = _to_float(space.get("cost"))
        if not code or cost is None or cost <= 0:
            continue
        if code not in table or cost < table[code]:
            table[code] = cost
    for name in SEAT_PREFERENCE.get(mode, ()):
        if name in table:
            return name, table[name]
    return "", None


def _parse_transit(transit: dict[str, Any], mode: str) -> dict[str, Any] | None:
    rails: list[dict[str, Any]] = []
    for segment in transit.get("segments") or []:
        rail = segment.get("railway") or {}
        trip = str(rail.get("trip") or "").strip()
        if not trip:
            continue
        is_high = _rail_prefix(trip) in HIGH_SPEED_PREFIX
        if mode == "high_speed" and not is_high:
            continue
        if mode == "train" and is_high:
            continue
        rails.append(rail)
    if not rails:
        return None

    price = 0.0
    trips: list[str] = []
    seats: list[str] = []
    for rail in rails:
        seat, value = _seat_price(rail.get("spaces"), mode)
        if value is None:
            return None
        price += value
        seats.append(seat)
        trips.append(str(rail.get("name") or rail.get("trip") or "").strip())

    duration = int(_to_float(transit.get("duration")) or 0) // 60
    if duration <= 0 or price <= 0:
        return None

    first, last = rails[0], rails[-1]
    return {
        "unit_price": round(price, 1),
        "duration_min": duration,
        "trips": trips,
        "seats": seats[:1] if len(set(seats)) == 1 else seats,
        "departure_stop": str((first.get("departure_stop") or {}).get("name") or ""),
        "arrival_stop": str((last.get("arrival_stop") or {}).get("name") or ""),
        "departure_time": str((first.get("departure_stop") or {}).get("time") or ""),
        "arrival_time": str((last.get("arrival_stop") or {}).get("time") or ""),
    }


def query_realtime(mode: str, origin_coord: tuple[float, float] | None,
                   dest_coord: tuple[float, float] | None) -> dict[str, Any] | None:
    """查高德 V3 实时铁路票价；拿不到返回 None（调用方回落估算）。"""
    if mode not in ("high_speed", "train"):
        return None
    if not config.AMAP_KEY or not origin_coord or not dest_coord:
        return None
    try:
        city1 = tools._citycode(origin_coord[0], origin_coord[1])
        city2 = tools._citycode(dest_coord[0], dest_coord[1])
    except Exception:  # noqa: BLE001 - 逆地理失败直接回落估算
        return None
    if not city1 or not city2:
        return None
    params = {
        "key": config.AMAP_KEY,
        "origin": f"{origin_coord[0]:.6f},{origin_coord[1]:.6f}",
        "destination": f"{dest_coord[0]:.6f},{dest_coord[1]:.6f}",
        "city1": city1,
        "city2": city2,
        "extensions": "all",
        "strategy": "0",
    }
    try:
        data = tools._get_json(AMAP_TRANSIT_URL, params, timeout=6, retries=0)
    except Exception:  # noqa: BLE001 - 网络/配额失败回落估算
        return None
    if str(data.get("status")) != "1":
        return None

    candidates = []
    for transit in (data.get("route") or {}).get("transits") or []:
        parsed = _parse_transit(transit, mode)
        if parsed:
            candidates.append(parsed)
    if not candidates:
        return None
    best = min(candidates, key=lambda one: one["duration_min"])
    best["queried_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    best["source"] = "amap"
    return best


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
          mode: str, note: str, coord: tuple[float, float] | None,
          tag: str = "估算") -> dict[str, Any]:
    label = mode_label(mode)
    name = ("去程" if kind == "outbound" else "返程") + f"：{origin}→{dest}（{label} · {tag}）"
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


def _leg(kind: str, mode: str, est: dict[str, Any], real: dict[str, Any] | None,
         people: int, origin: str, dest: str, coord: tuple[float, float] | None,
         nominal_start: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """生成一条去程/返程，返回 (item, 该段说明)。实时成功用真实票价与车次。"""
    if real:
        people_count = 1 if mode == "self_drive" else people
        total = round(real["unit_price"] * people_count, 1)
        duration = int(real["duration_min"])
        start = nominal_start
        departure = _minutes(f"{real.get('departure_time', '')[:2]}:{real.get('departure_time', '')[2:]}")
        if kind == "outbound" and departure is not None and 5 * 60 <= departure < 24 * 60:
            start = departure
        trip_text = " / ".join(real["trips"]) or "铁路班次"
        seat = real["seats"][0] if real["seats"] else "铁路座席"
        note = (
            f"高德实时票价：{trip_text}（{seat}）¥{real['unit_price']:.0f}/人，单程，"
            f"不含市内接驳与手续费；查询时间 {real['queried_at']}。"
        )
        item = _item(kind, origin, dest, start, duration, total, mode, note, coord, tag="实时")
        detail = {
            "direction": kind,
            "source": "amap",
            "unit_price": real["unit_price"],
            "total": total,
            "duration_min": duration,
            "trips": real["trips"],
            "seats": real["seats"],
            "departure_stop": real["departure_stop"],
            "arrival_stop": real["arrival_stop"],
            "departure_time": real["departure_time"],
            "arrival_time": real["arrival_time"],
            "queried_at": real["queried_at"],
        }
        return item, detail

    item = _item(kind, origin, dest, nominal_start, est["duration_min"], est["total"],
                 mode, est["note"], coord, tag="估算")
    detail = {
        "direction": kind,
        "source": "estimate",
        "unit_price": est["unit_price"],
        "total": est["total"],
        "duration_min": est["duration_min"],
    }
    return item, detail


def apply(itinerary: dict[str, Any], constraints: dict[str, Any], distance_km: float | None,
          origin_coord: tuple[float, float] | None = None,
          dest_coord: tuple[float, float] | None = None,
          realtime: bool = True) -> dict[str, Any]:
    """把去程/回程写进首末日，并裁掉首末日排满的景点。返回票价说明。"""
    days = itinerary.get("days") or []
    if not days:
        return {}
    straight = float(distance_km or 800.0)
    mode = resolve_mode(constraints.get("transport_mode"), straight)
    people = max(1, int(constraints.get("people") or 1))
    travelled_km = route_distance(mode, straight)
    est = estimate(mode, travelled_km, people)
    origin = str(constraints.get("origin") or "出发地")
    dest = str(constraints.get("city") or itinerary.get("city") or "目的地")

    real_out = query_realtime(mode, origin_coord, dest_coord) if realtime else None
    real_in = query_realtime(mode, dest_coord, origin_coord) if realtime else None

    outbound, out_detail = _leg("outbound", mode, est, real_out, people,
                                origin, dest, dest_coord, 8 * 60)
    inbound, in_detail = _leg("inbound", mode, est, real_in, people,
                              dest, origin, origin_coord, 14 * 60)

    arrival = _minutes(outbound["start"]) or 8 * 60
    arrival += est["duration_min"] if not real_out else int(real_out["duration_min"])
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

    sources = {out_detail["source"], in_detail["source"]}
    if sources == {"amap"}:
        source = "amap"
    elif "amap" in sources:
        source = "mixed"
    else:
        source = "estimate"
    queried = [one.get("queried_at") for one in (out_detail, in_detail) if one.get("queried_at")]
    info = {
        "mode": mode,
        "label": mode_label(mode),
        "distance_km": round(straight, 1),
        "route_km": travelled_km,
        "source": source,
        "estimated": source != "amap",
        "queried_at": queried[0] if queried else "",
        "unit_price": est["unit_price"],
        "outbound": out_detail["total"],
        "inbound": in_detail["total"],
        "total": round(out_detail["total"] + in_detail["total"], 1),
        "note": est["note"],
        "legs": [out_detail, in_detail],
    }
    itinerary["long_haul"] = info
    return info
