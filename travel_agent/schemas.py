"""工具 Schema（DeepSeek strict 模式）+ 参数 pydantic 校验 + 行程结构定义。"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import config

# --------------------------------------------------------------------------- #
# 3.3 工具 Schema：参数描述写全（类型/必填/何时用/格式示例）
# 注意：strict 模式要求 properties 全部 required 且 additionalProperties=false
# --------------------------------------------------------------------------- #
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "strict": config.STRICT_TOOLS,
            "description": "把地名/城市/地址解析成高德 GCJ-02 经纬度与城市名。"
            "当用户只说了目的地城市名、或需要城市中心坐标查天气时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_name": {
                        "type": "string",
                        "description": "要解析的地名，必须是真实地名。示例：'梅州市'、'广州塔'、'杭州市西湖区'",
                    }
                },
                "required": ["place_name"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_poi",
            "strict": config.STRICT_TOOLS,
            "description": "按关键词搜索指定城市内的景点/餐饮/住宿，返回名称、地址、GCJ-02 经纬度、"
            "营业时间(open_time)、票价(cost)。所有景点与价格必须来自本工具，禁止编造。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名或 adcode，用于限定搜索范围。示例：'梅州市'",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，一次只搜一类。示例：'景点'、'客家博物馆'、'特色餐厅'、'酒店'",
                    },
                    "count": {
                        "type": "integer",
                        "description": "返回条数，1-10。默认给 5",
                    },
                },
                "required": ["city", "keyword", "count"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "strict": config.STRICT_TOOLS,
            "description": "查询某坐标未来 N 天天气（最高/最低温、降水概率、天气码、是否适合户外）。"
            "排户外景点前必须调用，用来把户外景点排到晴天。",
            "parameters": {
                "type": "object",
                "properties": {
                    "lng": {"type": "number", "description": "经度，GCJ-02，必须在前。示例：116.12"},
                    "lat": {"type": "number", "description": "纬度，GCJ-02。示例：24.29"},
                    "days": {"type": "integer", "description": "查询天数 1-16，通常覆盖整个行程跨度"},
                },
                "required": ["lng", "lat", "days"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_route",
            "strict": config.STRICT_TOOLS,
            "description": "计算两点之间的交通距离(米)、耗时(秒)和费用(元)。"
            "排相邻景点顺序、判断当天是否太赶时必须调用。经纬度必须经度在前、纬度在后。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin_lng": {"type": "number", "description": "起点经度，GCJ-02。示例：116.434307"},
                    "origin_lat": {"type": "number", "description": "起点纬度，GCJ-02。示例：39.90909"},
                    "dest_lng": {"type": "number", "description": "终点经度，GCJ-02"},
                    "dest_lat": {"type": "number", "description": "终点纬度，GCJ-02"},
                    "mode": {
                        "type": "string",
                        "enum": ["driving", "walking", "transit"],
                        "description": "交通方式：driving=打车/自驾，walking=步行，transit=公交地铁",
                    },
                },
                "required": ["origin_lng", "origin_lat", "dest_lng", "dest_lat", "mode"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_budget",
            "strict": config.STRICT_TOOLS,
            "description": "按当前行程草稿计算预算明细（门票+交通+餐饮+住宿）。"
            "金额由代码计算，你只需要给出天数/人数/房间数和可选标准，禁止自己算钱。",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "行程天数，例如 2"},
                    "people": {"type": "integer", "description": "出行人数，例如 2"},
                    "rooms": {"type": "integer", "description": "房间数，按 2 人 1 间估算，例如 1"},
                    "food_per_person_per_day": {
                        "type": "number",
                        "description": "人均每日餐饮预算，默认 150；用户要求省钱时可调低",
                    },
                    "hotel_per_night": {
                        "type": "number",
                        "description": "每间每晚住宿预算，默认 200",
                    },
                },
                "required": [
                    "days",
                    "people",
                    "rooms",
                    "food_per_person_per_day",
                    "hotel_per_night",
                ],
                "additionalProperties": False,
            },
        },
    },
]


# --------------------------------------------------------------------------- #
# 工具参数校验（pydantic，防止模型输出错乱）
# --------------------------------------------------------------------------- #
class GeocodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    place_name: str = Field(..., min_length=1, description="地名")


class PoiArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = Field(..., min_length=1)
    keyword: str = Field(..., min_length=1)
    count: int = Field(5, ge=1, le=10)


class WeatherArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lng: float = Field(..., description="GCJ-02 经度，必须在前")
    lat: float = Field(..., description="GCJ-02 纬度")
    days: int = Field(3, ge=1, le=16)

    @field_validator("lng", "lat")
    @classmethod
    def _swap_guard(cls, value: float, info) -> float:
        """硬规则：国内坐标经度 73-136、纬度 3-54，超范围基本等于经纬度写反。"""
        bounds = (73.0, 136.0) if info.field_name == "lng" else (3.0, 54.0)
        if not bounds[0] <= value <= bounds[1]:
            raise ValueError(
                f"{info.field_name}={value} 超出国内范围 {bounds[0]}-{bounds[1]}，"
                "疑似经纬度写反或坐标系错误（高德必须经度在前、GCJ-02）"
            )
        return value


class RouteArgs(BaseModel):
    """起点/终点共 4 个坐标，全部走国内范围校验，防止经纬度写反。"""

    model_config = ConfigDict(extra="forbid")
    origin_lng: float
    origin_lat: float
    dest_lng: float
    dest_lat: float
    mode: Literal["driving", "walking", "transit"] = "driving"

    @field_validator("origin_lng", "dest_lng")
    @classmethod
    def _lng_range(cls, value: float, info) -> float:
        if not 73.0 <= value <= 136.0:
            raise ValueError(
                f"{info.field_name}={value} 超出国内经度范围 73-136，疑似经纬度写反或坐标系错误"
            )
        return value

    @field_validator("origin_lat", "dest_lat")
    @classmethod
    def _lat_range(cls, value: float, info) -> float:
        if not 3.0 <= value <= 54.0:
            raise ValueError(
                f"{info.field_name}={value} 超出国内纬度范围 3-54，疑似经纬度写反或坐标系错误"
            )
        return value


class BudgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(1, ge=1, le=10)
    people: int = Field(1, ge=1, le=20)
    rooms: int = Field(1, ge=1, le=10)
    food_per_person_per_day: float = Field(150.0, ge=0, le=5000)
    hotel_per_night: float = Field(200.0, ge=0, le=10000)


ARG_MODELS: dict[str, type[BaseModel]] = {
    "geocode": GeocodeArgs,
    "search_poi": PoiArgs,
    "get_weather": WeatherArgs,
    "plan_route": RouteArgs,
    "estimate_budget": BudgetArgs,
}


def validate_args(name: str, raw_args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """返回 (合法参数, None) 或 (None, 人类可读错误)。错误会回灌给模型，必须写清楚哪里错。"""
    model = ARG_MODELS.get(name)
    if model is None:
        return None, f"未知工具：{name}"
    try:
        return model.model_validate(raw_args).model_dump(), None
    except ValidationError as exc:
        details = "；".join(
            f"{'.'.join(str(part) for part in err['loc']) or '参数'}：{err['msg']}" for err in exc.errors()
        )
        return None, f"参数校验失败：{details}"
    except Exception as exc:  # noqa: BLE001 - 任何异常都转成可读文本
        return None, f"参数校验失败：{exc}"


# --------------------------------------------------------------------------- #
# 行程结构（模型最终输出的 JSON 必须符合它）
# --------------------------------------------------------------------------- #
ITEM_TYPES = {"attraction", "food", "hotel", "transport", "rest"}


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: str = Field(..., description="HH:MM")
    end: str = Field(..., description="HH:MM")
    type: str = Field("attraction")
    name: str = Field(..., min_length=1)
    lng: float | None = None
    lat: float | None = None
    cost: float = 0.0
    indoor: bool | None = None
    open_time: str = ""
    transport: str = "driving"
    walk_km: float | None = None
    note: str = ""

    @field_validator("type")
    @classmethod
    def _norm_type(cls, value: str) -> str:
        value = (value or "attraction").strip().lower()
        return value if value in ITEM_TYPES else "attraction"

    @field_validator("start", "end")
    @classmethod
    def _norm_time(cls, value: str) -> str:
        text = (value or "").strip().replace("：", ":")
        return text

    @field_validator("cost", mode="before")
    @classmethod
    def _norm_cost(cls, value: Any) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(str(value).replace("元", "").strip())
        except (TypeError, ValueError):
            return 0.0

    @field_validator("lng", "lat", "walk_km", mode="before")
    @classmethod
    def _norm_float(cls, value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class DayPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    day: int = 1
    date: str = ""
    weather_note: str = ""
    items: list[PlanItem] = Field(default_factory=list)


class Itinerary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    city: str = ""
    summary: str = ""
    days: list[DayPlan] = Field(default_factory=list)
    tips: list[str] = Field(default_factory=list)


JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str, require_days: bool = False) -> dict[str, Any] | None:
    """从模型回复里抠出 JSON 对象（容忍前后说明文字）。

    require_days=True 时只接受带 days 字段的整份行程，避免输出被截断后
    把某个「天」的片段误当成整份行程（否则会报出误导性的“行程没有任何内容”）。
    """
    if not text:
        return None
    candidates: list[str] = []
    match = JSON_FENCE.search(text)
    if match:
        candidates.append(match.group(1))
    start = text.find("{")
    while start != -1:
        candidates.append(text[start:])
        start = text.find("{", start + 1)
        if len(candidates) > 20:
            break
    decoder = json.JSONDecoder()
    fallback: dict[str, Any] | None = None
    for candidate in candidates:
        try:
            obj, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("days"), list):
            return obj
        if fallback is None:
            fallback = obj
    return None if require_days else fallback


def parse_itinerary(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """文本 -> 结构化行程。返回 (itinerary, None) 或 (None, 错误说明)。"""
    raw = extract_json(text, require_days=True)
    if raw is None:
        hint = "（JSON 可能被截断）" if len(text) > 1500 else ""
        return None, f"没有找到含 days 的合法 JSON 行程对象{hint}"
    try:
        return Itinerary.model_validate(raw).model_dump(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"行程 JSON 结构不符合要求：{str(exc).splitlines()[0]}"
