"""工具层：5 个工具（geocode / search_poi / get_weather / plan_route / estimate_budget）。

硬规则：
- 每个工具 10 秒超时、失败重试 2 次（1s -> 2s 指数退避）
- 失败返回结构化错误 {"success": False, "error": "..."}，绝不抛异常崩溃
- 高德接口坐标一律 GCJ-02 且【经度在前、纬度在后】
"""
from __future__ import annotations

import math
import time
from typing import Any

import requests

from . import config

AMAP_GEOCODE = "https://restapi.amap.com/v3/geocode/geo"
AMAP_REGEO = "https://restapi.amap.com/v3/geocode/regeo"
AMAP_POI_TEXT = "https://restapi.amap.com/v3/place/text"
ROUTE_URLS = {
    "driving": "https://restapi.amap.com/v5/direction/driving",
    "walking": "https://restapi.amap.com/v5/direction/walking",
    "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
}
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
SPEED_KMH = {"driving": 40.0, "walking": 4.8, "transit": 20.0}

_CITY_CACHE: dict[str, str] = {}


class ToolError(RuntimeError):
    """内部异常，仅用于工具内部流转，对外一律转成结构化错误。"""


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def _to_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "" or isinstance(value, (list, dict, bool)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(str(v) for v in value if isinstance(v, str)).strip()
    return ""


def _get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """10 秒超时 + 2 次指数退避重试。"""
    last_error: Exception | None = None
    for attempt in range(config.HTTP_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ToolError("接口返回格式异常")
            return data
        except Exception as exc:  # noqa: BLE001 - 统一兜底
            last_error = exc
            if attempt < config.HTTP_RETRIES:
                time.sleep(2 ** attempt)  # 1s -> 2s
    raise ToolError(str(last_error))


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _citycode(lng: float, lat: float) -> str:
    """公交规划需要 citycode，用逆地理编码取（带缓存，一次行程只多 1-2 次调用）。"""
    key = f"{round(lng, 2)},{round(lat, 2)}"
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]
    try:
        data = _get_json(AMAP_REGEO, {"key": config.AMAP_KEY, "location": f"{lng},{lat}"})
        component = (data.get("regeocode") or {}).get("addressComponent") or {}
        code = _text(component.get("citycode"))
    except Exception:  # noqa: BLE001 - 逆地理失败不阻塞主流程
        code = ""
    _CITY_CACHE[key] = code
    return code


# --------------------------------------------------------------------------- #
# 2.1 geocode
# --------------------------------------------------------------------------- #
def geocode(place_name: str) -> dict[str, Any]:
    """地名/地址 -> GCJ-02 经纬度 + 城市名。"""
    if not config.AMAP_KEY:
        return {"success": False, "error": "AMAP_KEY 未配置，请先填写 .env"}
    try:
        data = _get_json(AMAP_GEOCODE, {"key": config.AMAP_KEY, "address": place_name})
        geocodes = data.get("geocodes") or []
        if data.get("status") != "1":
            # 把高德的原始原因透出来（INVALID_USER_KEY / USERKEY_PLAT_NOMATCH / QUOTA_PLAN_RUN_OUT…）
            info = _text(data.get("info")) or "未知原因"
            return {"success": False, "error": f"地名解析失败：{info}"}
        if not geocodes:
            return {"success": False, "error": "地名解析失败，请检查输入"}
        item = geocodes[0]
        location = _text(item.get("location")).split(",")
        if len(location) != 2:
            return {"success": False, "error": "地名解析失败，请检查输入"}
        return {
            "success": True,
            "name": _text(item.get("formatted_address")) or place_name,
            "lng": round(float(location[0]), 6),  # 经度在前
            "lat": round(float(location[1]), 6),  # 纬度在后
            "city": _text(item.get("city")) or _text(item.get("province")),
            "adcode": _text(item.get("adcode")),
            "coordinate_system": "GCJ-02",
        }
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"地名解析失败，请检查输入（{exc}）"}


# --------------------------------------------------------------------------- #
# 2.2 search_poi
# --------------------------------------------------------------------------- #
def search_poi(city: str, keyword: str, count: int = 5) -> dict[str, Any]:
    """按关键词搜城市内景点/餐饮/住宿，返回基础信息。"""
    if not config.AMAP_KEY:
        return {"success": False, "error": "AMAP_KEY 未配置，请先填写 .env"}
    try:
        count = max(1, min(int(count or 5), 10))
        data = _get_json(
            AMAP_POI_TEXT,
            {
                "key": config.AMAP_KEY,
                "keywords": keyword,
                "city": city,
                "citylimit": "true",
                "offset": count,
                "page": 1,
                "extensions": "all",
            },
        )
        if data.get("status") != "1":
            info = _text(data.get("info")) or "未知原因"
            return {"success": False, "error": f"POI搜索失败：{info}"}
        pois: list[dict[str, Any]] = []
        for raw in (data.get("pois") or [])[:count]:
            location = _text(raw.get("location")).split(",")
            if len(location) != 2:
                continue
            biz = raw.get("biz_ext") if isinstance(raw.get("biz_ext"), dict) else {}
            pois.append(
                {
                    "name": _text(raw.get("name")),
                    "address": _text(raw.get("address")),
                    "lng": round(float(location[0]), 6),
                    "lat": round(float(location[1]), 6),
                    "tel": _text(raw.get("tel")),
                    "open_time": _text(biz.get("open_time")),
                    "cost": _to_float(biz.get("cost")),
                    "type": _text(raw.get("type")),
                }
            )
        if not pois:
            return {"success": False, "error": "POI搜索失败，换个关键词试试"}
        return {"success": True, "city": city, "keyword": keyword, "count": len(pois), "pois": pois}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"POI搜索失败，换个关键词试试（{exc}）"}


# --------------------------------------------------------------------------- #
# 2.3 get_weather（Open-Meteo，WGS-84 区域级数据，无需坐标转换）
# --------------------------------------------------------------------------- #
def get_weather(lng: float, lat: float, days: int = 3) -> dict[str, Any]:
    """查坐标未来 N 天天气；失败时默认按晴天规划（校验层会跳过天气校验）。"""
    try:
        days = max(1, min(int(days or 3), 16))
        data = _get_json(
            OPEN_METEO,
            {
                "latitude": lat,  # Open-Meteo 要求 latitude 在前
                "longitude": lng,
                "daily": "temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,weathercode",
                "timezone": "Asia/Shanghai",
                "forecast_days": days,
            },
        )
        daily = data.get("daily") or {}
        dates = daily.get("time") or []
        result: list[dict[str, Any]] = []
        for idx, day in enumerate(dates):
            code = int(_to_float((daily.get("weathercode") or [])[idx], 0) or 0)
            prob = int(_to_float((daily.get("precipitation_probability_max") or [])[idx], 0) or 0)
            result.append(
                {
                    "date": day,
                    "max_temp": _to_float((daily.get("temperature_2m_max") or [])[idx]),
                    "min_temp": _to_float((daily.get("temperature_2m_min") or [])[idx]),
                    "rain_prob": prob,
                    "weathercode": code,
                    "is_rainy": code >= 51,  # >=51 为小雨及以上
                    "is_outdoor_ok": prob < config.RAIN_PROB_THRESHOLD,
                }
            )
        if not result:
            raise ToolError("天气数据为空")
        return {"success": True, "days": result, "timezone": data.get("timezone", "Asia/Shanghai")}
    except Exception:  # noqa: BLE001
        return {"success": False, "error": "天气查询失败，默认按晴天规划"}


# --------------------------------------------------------------------------- #
# 2.4 plan_route
# --------------------------------------------------------------------------- #
def _route_cost(mode: str, distance_m: float, path: dict[str, Any]) -> float:
    km = distance_m / 1000.0
    if mode == "walking":
        return 0.0
    cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
    if mode == "driving":
        taxi = _to_float(cost.get("taxi_fee"))
        return round(taxi if taxi is not None else max(10.0, km * 2.5), 1)
    fee = _to_float(cost.get("transit_fee")) or _to_float(cost.get("ticket_price"))
    return round(fee if fee is not None else max(2.0, km * 0.3), 1)


def _pick_path(data: dict[str, Any], mode: str) -> dict[str, Any]:
    """从高德 V5 响应里取第一条路径：driving/walking 在 route.paths，transit 在 route.transits。"""
    route = data.get("route") if isinstance(data.get("route"), dict) else {}
    items = route.get("transits") if mode == "transit" else route.get("paths")
    if not items:
        raise ToolError("无可用路径")
    first = items[0]
    if not isinstance(first, dict):
        raise ToolError("无可用路径")
    return first


def _path_duration(path: dict[str, Any]) -> float | None:
    """V5 把 duration 放在 path.cost 里（单位秒），同时兼容顶层写法。"""
    duration = _to_float(path.get("duration"))
    if duration is not None:
        return duration
    cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
    return _to_float(cost.get("duration"))


def plan_route(
    origin_lng: float,
    origin_lat: float,
    dest_lng: float,
    dest_lat: float,
    mode: str = "driving",
) -> dict[str, Any]:
    """两点间距离（米）+ 耗时（秒）+ 费用；接口失败自动降级为直线*1.3 估算，不卡流程。"""
    mode = mode if mode in ROUTE_URLS else "driving"
    straight = haversine_m(origin_lng, origin_lat, dest_lng, dest_lat)
    degraded_distance = straight * 1.3
    degraded_duration = degraded_distance / (SPEED_KMH[mode] * 1000 / 3600) + (900 if mode == "transit" else 0)
    if not config.AMAP_KEY:
        return {
            "success": False,
            "mode": mode,
            "error": "AMAP_KEY 未配置，已按直线距离估算",
            "distance_m": round(degraded_distance),
            "duration_sec": round(degraded_duration),
            "cost_yuan": _route_cost(mode, degraded_distance, {}),
            "estimated": True,
        }
    try:
        params: dict[str, Any] = {
            "key": config.AMAP_KEY,
            "origin": f"{origin_lng:.6f},{origin_lat:.6f}",  # 经度在前
            "destination": f"{dest_lng:.6f},{dest_lat:.6f}",
            "show_fields": "cost",
        }
        if mode == "transit":
            params["city1"] = _citycode(origin_lng, origin_lat)
            params["city2"] = _citycode(dest_lng, dest_lat)
            if not params["city1"] or not params["city2"]:
                raise ToolError("公交规划缺少城市信息")
        data = _get_json(ROUTE_URLS[mode], params)
        if data.get("status") != "1":
            raise ToolError(_text(data.get("info")) or "路径规划失败")
        first = _pick_path(data, mode)
        distance = _to_float(first.get("distance"))
        duration = _path_duration(first)
        if distance is None or duration is None:
            raise ToolError("路径数据缺失")
        cost_yuan = _route_cost(mode, distance, first)
        if mode == "driving":
            # V5 在 route 顶层给了出租车预估，比自己按公里数估更准
            taxi_cost = _to_float((data.get("route") or {}).get("taxi_cost"))
            if taxi_cost is not None:
                cost_yuan = round(taxi_cost, 1)
        return {
            "success": True,
            "mode": mode,
            "distance_m": round(distance),
            "duration_sec": round(duration),
            "cost_yuan": cost_yuan,
            "estimated": False,
        }
    except Exception as exc:  # noqa: BLE001 - 降级估算
        return {
            "success": False,
            "mode": mode,
            "error": f"路径规划失败，已按直线距离估算（{exc}）",
            "distance_m": round(degraded_distance),
            "duration_sec": round(degraded_duration),
            "cost_yuan": _route_cost(mode, degraded_distance, {}),
            "estimated": True,
        }


# --------------------------------------------------------------------------- #
# 2.5 estimate_budget（纯代码计算，模型只传参数、不做算术）
# --------------------------------------------------------------------------- #
def estimate_budget(
    itinerary: dict[str, Any],
    days: int | None = None,
    people: int | None = 1,
    rooms: int | None = None,
    food_per_person_per_day: float | None = None,
    hotel_per_night: float | None = None,
) -> dict[str, Any]:
    """总预算 = 门票 + 交通 + 餐饮(150/人/天) + 住宿(200/间/晚)。"""
    try:
        itinerary = itinerary or {}
        people = max(1, int(people or 1))
        day_list = itinerary.get("days") or []
        days = int(days or len(day_list) or 1)
        rooms = int(rooms or math.ceil(people / 2))
        food_rate = _to_float(food_per_person_per_day, config.FOOD_PER_PERSON_PER_DAY) or 150.0
        hotel_rate = _to_float(hotel_per_night, config.HOTEL_PER_NIGHT) or 200.0

        ticket = 0.0
        transport = 0.0
        for day in day_list:
            for item in day.get("items") or []:
                cost = _to_float(item.get("cost"), 0.0) or 0.0
                kind = item.get("type")
                if kind == "attraction":
                    ticket += cost
                elif kind == "transport":
                    transport += cost
        nights = max(days - 1, 0)
        food = food_rate * people * days
        hotel = hotel_rate * rooms * nights
        return {
            "success": True,
            "total": round(ticket + transport + food + hotel, 1),
            "breakdown": {
                "ticket": round(ticket, 1),
                "transport": round(transport, 1),
                "food": round(food, 1),
                "hotel": round(hotel, 1),
            },
            "meta": {
                "days": days,
                "people": people,
                "rooms": rooms,
                "nights": nights,
                "food_per_person_per_day": food_rate,
                "hotel_per_night": hotel_rate,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"预算计算失败：{exc}",
            "total": 0,
            "breakdown": {"ticket": 0, "transport": 0, "food": 0, "hotel": 0},
        }


# --------------------------------------------------------------------------- #
# 统一入口
# --------------------------------------------------------------------------- #
_TOOL_FUNCS = {
    "geocode": geocode,
    "search_poi": search_poi,
    "get_weather": get_weather,
    "plan_route": plan_route,
}


def execute_tool(name: str, args: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
    """按名字执行工具（args 已过 pydantic 校验）；estimate_budget 自动注入当前行程草稿。"""
    try:
        if name == "estimate_budget":
            payload = dict(args or {})
            payload.pop("itinerary", None)
            return estimate_budget(draft or {}, **payload)
        func = _TOOL_FUNCS.get(name)
        if func is None:
            return {"success": False, "error": f"未知工具：{name}"}
        return func(**(args or {}))
    except TypeError as exc:
        return {"success": False, "error": f"工具参数错误：{exc}"}
    except Exception as exc:  # noqa: BLE001 - 任何异常都不许崩
        return {"success": False, "error": f"工具执行异常：{exc}"}
