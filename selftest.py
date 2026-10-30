"""离线自检脚本：不需要任何 API Key，也不访问网络。

覆盖：
1. 预算计算（纯代码）
2. 校验层 6 条规则（天气 / 耗时 / 预算 / 营业时间 / 同行人 / 重复景点）
3. 坐标写反守卫、行程 JSON 解析容错
4. Agent 自纠错闭环（假 LLM 两轮：首轮出错 -> 校验 -> 重排 -> 通过）
5. Markdown 行程单渲染

运行：python selftest.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from travel_agent import agent, config, schemas, tools, validator

TODAY = date.today()
START = (TODAY + timedelta(days=1)).isoformat()
D2 = (TODAY + timedelta(days=2)).isoformat()

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, bool(ok), detail))


# --------------------------------------------------------------------------- #
# 假工具（离线替身高德 / Open-Meteo）
# --------------------------------------------------------------------------- #
REAL_ESTIMATE = tools.estimate_budget


def make_weather(rain_on: str = START):
    def weather(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_weather":
            total = max(1, min(int(args.get("days") or 3), 16))
            days = []
            for offset in range(total):
                current = (TODAY + timedelta(days=offset)).isoformat()
                rain_prob = 70 if current == rain_on else 5
                rainy = rain_prob >= config.RAIN_PROB_THRESHOLD
                days.append(
                    {
                        "date": current,
                        "max_temp": 26,
                        "min_temp": 18,
                        "rain_prob": rain_prob,
                        "weathercode": 61 if rainy else 1,
                        "is_rainy": rainy,
                        "is_outdoor_ok": not rainy,
                    }
                )
            return {"success": True, "days": days, "timezone": "Asia/Shanghai"}
        if name == "plan_route":
            return {
                "success": True,
                "mode": args.get("mode", "driving"),
                "distance_m": 9000,
                "duration_sec": 1800,
                "cost_yuan": 30,
                "estimated": False,
            }
        return {"success": False, "error": f"未覆盖的工具：{name}"}

    return weather


def install_fakes(rain_on: str = START) -> None:
    """把 tools.execute_tool 换成离线版本（validator 与 agent 共用同一条通路）。"""
    weather = make_weather(rain_on)

    def fake_execute(name: str, args: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
        if name == "estimate_budget":
            payload = {k: v for k, v in (args or {}).items() if k != "itinerary"}
            return REAL_ESTIMATE(draft or {}, **payload)
        if name == "geocode":
            return {"success": True, "name": args.get("place_name"), "lng": 116.12, "lat": 24.29, "city": "梅州市"}
        if name == "search_poi":
            return {"success": True, "city": args.get("city"), "keyword": args.get("keyword"), "count": 1, "pois": []}
        return weather(name, args)

    tools.execute_tool = fake_execute


# --------------------------------------------------------------------------- #
# 行程构造小工具
# --------------------------------------------------------------------------- #
def item(
    name: str,
    start: str,
    end: str,
    cost: float = 0.0,
    outdoor: bool = True,
    open_time: str = "08:00-18:00",
    kind: str = "attraction",
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "type": kind,
        "name": name,
        "lng": 116.407,
        "lat": 24.276,
        "cost": cost,
        "indoor": not outdoor,
        "open_time": open_time,
        "transport": "driving",
        "walk_km": 1.5,
        "note": "",
    }


def day(no: int, date_str: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"day": no, "date": date_str, "weather_note": "", "items": items}


def itinerary(days: list[dict[str, Any]], city: str = "梅州市") -> dict[str, Any]:
    return {"city": city, "summary": "自检行程", "days": days, "tips": ["携带雨具"]}


def constraints(**overrides: Any) -> dict[str, Any]:
    raw = {
        "city": "梅州市",
        "days": 2,
        "people": 2,
        "budget": 1000,
        "start_date": START,
        "companions": "带老人",
        "preferences": "轻松一点",
        "request": "想去客家文化相关的地方",
    }
    raw.update(overrides)
    built = agent.build_constraints(raw)
    built["lng"], built["lat"] = 116.12, 24.29
    return built


def issues_of(plan: dict[str, Any], cons: dict[str, Any] | None = None) -> list[str]:
    return validator.validate(plan, cons or constraints(), router=tools.execute_tool)


def has_issue(issues: list[str], *keywords: str) -> bool:
    return any(all(word in issue for word in keywords) for issue in issues)


# --------------------------------------------------------------------------- #
# 行程样本
# --------------------------------------------------------------------------- #
BAD_PLAN = itinerary(
    [
        day(
            1,
            START,
            [
                item("雁南飞茶田", "09:00", "11:00", 300),
                item("客家公园", "11:30", "13:30", 200),
                item("千佛塔", "14:30", "16:30", 100),
            ],
        ),
        day(2, D2, [item("雁南飞茶田", "09:00", "11:00", 300)]),
    ]
)

GOOD_PLAN = itinerary(
    [
        day(
            1,
            START,
            [
                item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False),
                item("客家美术馆", "13:30", "15:30", 20, outdoor=False),
            ],
        ),
        day(
            2,
            D2,
            [
                item("雁南飞茶田", "09:00", "10:30", 60),
                item("客家公园", "13:00", "14:30", 0),
            ],
        ),
    ]
)


def as_text(plan: dict[str, Any], preamble: str = "好的，行程如下：") -> str:
    return f"{preamble}\n```json\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n```\n希望有帮助。"


# --------------------------------------------------------------------------- #
# 1. 预算（纯代码）
# --------------------------------------------------------------------------- #
def test_budget() -> None:
    plan = itinerary(
        [
            day(
                1,
                "2026-01-01",
                [
                    item("A", "09:00", "11:00", 60),
                    item("B", "13:00", "15:00", 50),
                    item("打车", "15:00", "15:20", 30, kind="transport"),
                ],
            ),
            day(2, "2026-01-02", [item("C", "09:00", "11:00", 0)]),
        ]
    )
    budget = tools.estimate_budget(
        plan, days=2, people=2, rooms=1, food_per_person_per_day=150, hotel_per_night=200
    )
    breakdown = budget["breakdown"]
    check(
        "预算计算：门票/交通/餐饮/住宿",
        budget["success"]
        and budget["total"] == 940
        and breakdown == {"ticket": 110, "transport": 30, "food": 600, "hotel": 200},
        f"total={budget['total']} breakdown={breakdown}",
    )
    check("预算计算：2 天=1 晚住宿", budget["meta"]["nights"] == 1, f"nights={budget['meta']['nights']}")

    bad = tools.estimate_budget(plan, days=0, people="x")
    check("预算计算：非法入参不抛异常", bad["success"] is False and "error" in bad, str(bad.get("error"))[:60])


# --------------------------------------------------------------------------- #
# 2. 六条校验规则
# --------------------------------------------------------------------------- #
def test_weather_rule() -> None:
    plan = itinerary([day(1, START, [item("雁南飞茶田", "09:00", "11:00", 60)])])
    issues = issues_of(plan, constraints(days=1, budget=10000))
    check("校验-天气：雨天排户外景点被拦截", has_issue(issues, "降水概率", "户外景点"), "；".join(issues)[:120])

    indoor_plan = itinerary([day(1, START, [item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False)])])
    indoor_issues = issues_of(indoor_plan, constraints(days=1, budget=10000))
    check("校验-天气：雨天排室内景点通过", not has_issue(indoor_issues, "降水概率"), "；".join(indoor_issues)[:120])


def test_duration_rule() -> None:
    plan = itinerary(
        [
            day(
                1,
                D2,
                [
                    item("景点1", "09:00", "11:00"),
                    item("景点2", "11:30", "13:30"),
                    item("景点3", "14:00", "16:00"),
                    item("景点4", "16:30", "18:30"),
                ],
            )
        ]
    )
    issues = issues_of(plan, constraints(days=1, budget=100000, companions=""))
    check("校验-耗时：4 个景点超过 8 小时被拦截", has_issue(issues, "总耗时", "超过当日可用"), "；".join(issues)[:120])


def test_dwell_target_advice() -> None:
    """超时反馈给出的压缩配额必须同时考虑「游览软上限」，不能只贴着 8 小时总时长。"""
    plan = itinerary(
        [
            day(
                1,
                D2,
                [
                    item("景点1", "09:00", "11:05"),
                    item("景点2", "11:40", "13:45"),
                    item("景点3", "14:20", "16:25"),
                ],
            )
        ]
    )
    issues = issues_of(plan, constraints(days=1, budget=100000, companions="带老人"))
    check(
        "校验-超时配额：带老人时目标压到 4 小时以内",
        has_issue(issues, "游览总时长", "游览上限 4h"),
        "；".join(issues)[:160],
    )


def test_budget_rule() -> None:
    plan = itinerary([day(1, D2, [item("收费景点", "09:00", "11:00", 500)])])
    issues = issues_of(plan, constraints(days=1, budget=300, companions=""))
    check("校验-预算：总预算超限被拦截", has_issue(issues, "超过用户设定的", "预算"), "；".join(issues)[:120])


def test_opening_rule() -> None:
    plan = itinerary(
        [day(1, D2, [item("开馆晚的馆", "09:00", "11:00", 0, outdoor=False, open_time="10:00-17:00")])]
    )
    issues = issues_of(plan, constraints(days=1, budget=10000, companions=""))
    check("校验-营业时间：早于开馆时间被拦截", has_issue(issues, "不在开放时段内"), "；".join(issues)[:120])

    monday = TODAY + timedelta(days=((7 - TODAY.weekday()) % 7) or 7)
    closed_plan = itinerary(
        [
            day(
                1,
                monday.isoformat(),
                [item("周一闭馆的馆", "10:00", "12:00", 0, outdoor=False, open_time="周一闭馆 10:00-17:00")],
            )
        ]
    )
    closed_issues = issues_of(
        closed_plan, constraints(days=1, budget=10000, companions="", start_date=monday.isoformat())
    )
    check("校验-营业时间：周一闭馆被拦截", has_issue(closed_issues, "闭馆"), "；".join(closed_issues)[:120])


def test_companion_rule() -> None:
    plan = itinerary(
        [
            day(
                1,
                D2,
                [
                    item("景点1", "09:00", "10:00"),
                    item("景点2", "10:30", "11:30"),
                    item("景点3", "13:00", "14:00"),
                    item("景点4", "14:30", "15:30"),
                ],
            )
        ]
    )
    issues = issues_of(plan, constraints(days=1, budget=100000))
    check("校验-同行人：老人同行单日景点数超限", has_issue(issues, "同行有老人"), "；".join(issues)[:120])
    check("校验-同行人：老人同行步行距离超限", has_issue(issues, "步行距离"), "；".join(issues)[:120])


def test_duplicate_rule() -> None:
    plan = itinerary(
        [
            day(
                1,
                D2,
                [
                    item("某某古镇", "09:00", "11:00", 0, outdoor=False),
                    item("某某古镇", "13:00", "15:00", 0, outdoor=False),
                ],
            )
        ]
    )
    issues = issues_of(plan, constraints(days=1, budget=100000, companions=""))
    check("校验-重复景点：同名景点被拦截", has_issue(issues, "出现 2 次"), "；".join(issues)[:120])


def test_plan_level() -> None:
    good_issues = issues_of(GOOD_PLAN, constraints())
    check("校验-合规行程：0 个问题", good_issues == [], "；".join(good_issues)[:160])

    bad_issues = issues_of(BAD_PLAN, constraints())
    hits = {
        "天气": has_issue(bad_issues, "降水概率"),
        "预算": has_issue(bad_issues, "超过用户设定的"),
        "重复": has_issue(bad_issues, "出现 2 次"),
    }
    check(
        "校验-问题行程：同时命中天气/预算/重复",
        all(hits.values()) and len(bad_issues) >= 5,
        f"{hits} issues={len(bad_issues)}",
    )


# --------------------------------------------------------------------------- #
# 3. 解析 / 参数防线 / 渲染
# --------------------------------------------------------------------------- #
def test_parsing() -> None:
    parsed, err = schemas.parse_itinerary(as_text(GOOD_PLAN))
    check("解析：带说明文字的 JSON 可解析", parsed is not None and len(parsed["days"]) == 2, str(err))

    parsed_raw, _ = schemas.parse_itinerary(json.dumps(GOOD_PLAN, ensure_ascii=False))
    check("解析：裸 JSON 可解析", parsed_raw is not None and parsed_raw["city"] == "梅州市")

    failed, err2 = schemas.parse_itinerary("我想想，大概去梅州吧，还没定。")
    check("解析：没有 JSON 时返回可读错误", failed is None and bool(err2), str(err2))

    odd = {
        "city": "梅州市",
        "days": [
            {
                "day": 1,
                "date": D2,
                "items": [{"start": "9：00", "end": "11:00", "type": "sight", "name": "某地", "cost": "60元"}],
            }
        ],
    }
    normalized, err3 = schemas.parse_itinerary(json.dumps(odd, ensure_ascii=False))
    first = normalized["days"][0]["items"][0] if normalized else {}
    check(
        "解析：中文冒号 / 非法 type / 带单位金额被归一",
        normalized is not None
        and first.get("start") == "9:00"
        and first.get("type") == "attraction"
        and first.get("cost") == 60.0,
        f"{first} {err3}",
    )


def test_coord_guard() -> None:
    args, err = schemas.validate_args("get_weather", {"lng": 24.29, "lat": 116.12, "days": 2})
    check("参数防线：天气接口经纬度写反被拒绝", args is None and "疑似" in (err or ""), str(err)[:100])

    args2, err2 = schemas.validate_args(
        "plan_route",
        {"origin_lng": 116.1, "origin_lat": 24.2, "dest_lng": 24.3, "dest_lat": 116.4, "mode": "driving"},
    )
    check("参数防线：路径规划经纬度写反被拒绝", args2 is None, str(err2)[:100])

    ok, err3 = schemas.validate_args("get_weather", {"lng": 116.12, "lat": 24.29, "days": 2})
    check("参数防线：正确坐标通过", ok is not None and ok["days"] == 2, str(err3))

    bad, err4 = schemas.validate_args("search_poi", {"city": "梅州", "keyword": "景点", "count": 99})
    check("参数防线：count 越界被拒绝", bad is None, str(err4)[:100])

    missing, err5 = schemas.validate_args("geocode", {})
    check("参数防线：缺必填参数被拒绝", missing is None, str(err5)[:100])


def test_markdown() -> None:
    cons = constraints()
    result = {
        "status": "ok",
        "constraints": cons,
        "itinerary": GOOD_PLAN,
        "budget": tools.estimate_budget(GOOD_PLAN, days=2, people=2, rooms=1),
        "issues": [],
        "iterations": 2,
        "tool_calls": 5,
        "elapsed": 1.2,
        "trace": [{"line": "[准备] 自检"}],
    }
    md = agent.render_markdown(result)
    check(
        "渲染：Markdown 含每日表格 / 预算明细 / Trace",
        "## 预算明细" in md and "## 执行 Trace" in md and "| 时间 |" in md and "第1天" in md and "总预算" in md,
        md.splitlines()[0] if md else "空",
    )


# --------------------------------------------------------------------------- #
# 4. Agent 自纠错闭环（假 LLM）
# --------------------------------------------------------------------------- #
def test_agent_self_correction() -> None:
    replies = [as_text(BAD_PLAN), as_text(GOOD_PLAN)]
    seen: list[list[dict[str, Any]]] = []
    counter = {"index": 0}

    def fake_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any):
        seen.append([dict(message) for message in messages])
        index = min(counter["index"], len(replies) - 1)
        counter["index"] += 1
        return {"role": "assistant", "content": replies[index]}, {"prompt_tokens": 900, "completion_tokens": 300}

    traces: list[dict[str, Any]] = []
    engine = agent.TravelAgent(trace_callback=traces.append, chat=fake_chat)
    result = engine.run(
        "想去客家文化相关的地方，行程轻松一点",
        {"city": "梅州市", "days": 2, "people": 2, "budget": 1000, "start_date": START, "companions": "带老人"},
    )

    check(
        "Agent：首轮失败后重排并最终通过",
        result["status"] == "ok",
        f"status={result['status']} issues={result['issues']}",
    )
    check("Agent：迭代 2 轮（一次重排）", result["iterations"] == 2, f"iterations={result['iterations']}")
    check(
        "Agent：问题以自然语言回灌给模型",
        len(seen) >= 2
        and any(
            message.get("role") == "user" and "行程有以下问题" in str(message.get("content"))
            for message in seen[1]
        ),
        f"第二轮消息数={len(seen[1]) if len(seen) > 1 else 0}",
    )
    check(
        "Agent：Trace 同时记录校验问题与工具调用",
        any(entry["kind"] == "issue" for entry in result["trace"])
        and any(entry["kind"] == "tool_call" for entry in result["trace"]),
        f"trace={len(result['trace'])}",
    )
    check("Agent：预算由代码计算且未超限", result["budget"]["total"] <= 1000, str(result["budget"].get("total")))
    check(
        "Agent：Trace 回调收到全部条目",
        len(traces) == len(result["trace"]) and len(traces) > 0,
        f"{len(traces)}",
    )
    check("Agent：返回的 Markdown 可导出", "## 预算明细" in result["markdown"], "缺少预算明细")


def test_agent_max_iterations() -> None:
    """模型始终输出有问题的行程时，必须在 MAX_ITERATIONS 内停下，不能死循环。"""

    def stubborn_chat(messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any):
        return {"role": "assistant", "content": as_text(BAD_PLAN)}, {}

    engine = agent.TravelAgent(chat=stubborn_chat)
    result = engine.run(
        "随便规划",
        {"city": "梅州市", "days": 2, "people": 2, "budget": 1000, "start_date": START, "companions": "带老人"},
    )
    check(
        "Agent：问题无法修复时不会死循环，并保留问题清单",
        result["status"] == "max_iterations"
        and 1 <= result["iterations"] <= config.MAX_ITERATIONS
        and len(result["issues"]) > 0,
        f"status={result['status']} iterations={result['iterations']} issues={len(result['issues'])}",
    )
    check(
        "Agent：同一组问题反复出现时提前止损",
        result["iterations"] < config.MAX_ITERATIONS,
        f"iterations={result['iterations']}（上限 {config.MAX_ITERATIONS}）",
    )


def test_ui_apptest() -> None:
    """界面冒烟：用 Streamlit AppTest 跑一遍 app.py（不联网）。缺 Key 时应优雅报错而非白屏。"""
    try:
        from streamlit.testing.v1 import AppTest
    except Exception:  # noqa: BLE001 - 未安装 streamlit 时跳过，不算失败
        check("界面：跳过（未安装 streamlit）", True)
        return

    page = str(Path(__file__).resolve().parent / "app.py")
    # 界面用例只验证「缺 Key 时的降级行为」，与本地 .env 是否已填 Key 无关：
    # 临时清空 Key，避免自检跑去真实调用（既慢又花钱）
    real_keys = (config.DEEPSEEK_API_KEY, config.AMAP_KEY)
    config.DEEPSEEK_API_KEY, config.AMAP_KEY = "", ""
    try:
        page_app = AppTest.from_file(page, default_timeout=90).run()
        check("界面：首屏无异常", not page_app.exception, str([e.value for e in page_app.exception]))
        check("界面：缺少 Key 时给出提示", any("缺少环境变量" in w.value for w in page_app.warning))

        page_app.button[0].click().run()
        check(
            "界面：无 Key 点击后不白屏",
            not page_app.exception and bool(page_app.error),
            str([e.value for e in page_app.error]),
        )
    finally:
        config.DEEPSEEK_API_KEY, config.AMAP_KEY = real_keys

    cons = constraints()
    result = {
        "status": "ok",
        "iterations": 2,
        "tool_calls": 5,
        "elapsed": 3.2,
        "issues": [],
        "constraints": cons,
        "itinerary": GOOD_PLAN,
        "budget": tools.estimate_budget(GOOD_PLAN, cons["days"], cons["people"]),
        "trace": [{"kind": "decision", "line": "第 1 轮：调用工具 search_poi"}],
    }
    result["markdown"] = agent.render_markdown(result)
    page_app.session_state["result"] = result
    page_app.run()
    check("界面：行程渲染无异常", not page_app.exception, str([e.value for e in page_app.exception]))
    check("界面：行程表与预算表都渲染", len(page_app.dataframe) >= 2, f"{len(page_app.dataframe)} 个表格")


def test_route_v5_parsing() -> None:
    """回归：高德 V5 把 duration 放在 path.cost 里（driving/walking 用 paths，transit 用 transits）。

    之前按顶层 path["duration"] 取值，真实接口永远取不到 -> 静默降级成直线估算，
    耗时校验形同虚设。这里用真实响应结构的精简样本锁死解析逻辑。
    """

    def fake_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
        payloads: dict[str, dict[str, Any]] = {
            tools.AMAP_REGEO: {"status": "1", "regeocode": {"addressComponent": {"citycode": "0753"}}},
            tools.ROUTE_URLS["driving"]: {
                "status": "1",
                "info": "OK",
                "route": {"taxi_cost": "23", "paths": [{"distance": "8620", "cost": {"duration": "1209"}}]},
            },
            tools.ROUTE_URLS["walking"]: {
                "status": "1",
                "route": {"paths": [{"distance": "7832", "cost": {"duration": "6266"}}]},
            },
            tools.ROUTE_URLS["transit"]: {
                "status": "1",
                "route": {"transits": [{"distance": "10717", "cost": {"duration": "5011", "transit_fee": "2.0"}}]},
            },
        }
        return payloads[url]

    real_get_json = tools._get_json
    real_key = config.AMAP_KEY
    tools._get_json = fake_get_json
    config.AMAP_KEY = "test-key"
    try:
        driving = tools.plan_route(116.12, 24.29, 116.18, 24.31, "driving")
        walking = tools.plan_route(116.12, 24.29, 116.18, 24.31, "walking")
        transit = tools.plan_route(116.12, 24.29, 116.18, 24.31, "transit")
    finally:
        tools._get_json = real_get_json
        config.AMAP_KEY = real_key

    check(
        "路径解析：V5 driving 取 cost.duration 且不再降级",
        driving.get("success") is True and driving.get("duration_sec") == 1209 and driving.get("estimated") is False,
        str(driving),
    )
    check("路径解析：driving 优先用 route.taxi_cost 估费用", driving.get("cost_yuan") == 23, str(driving))
    check(
        "路径解析：V5 walking 免费且时长正确",
        walking.get("duration_sec") == 6266 and walking.get("cost_yuan") == 0,
        str(walking),
    )
    check(
        "路径解析：V5 transit 从 route.transits 取数",
        transit.get("distance_m") == 10717
        and transit.get("duration_sec") == 5011
        and transit.get("cost_yuan") == 2.0,
        str(transit),
    )


def test_leg_transport() -> None:
    """回归：模型只在景点写 transport 方式、不生成交通条目时，交通费必须由代码补进预算。"""
    engine = agent.TravelAgent(trace_callback=lambda e: None, chat=lambda *a, **k: ({}, {}))
    engine.state = {"tool_results_cache": {}, "trace_log": []}
    engine.round_no = 1

    plan = itinerary(
        [day(1, START, [item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False), item("客家美术馆", "13:30", "15:30", 20, outdoor=False)])]
    )
    cost, notes = engine._leg_transport(plan)
    check("交通费补算：按相邻点位累加 plan_route 费用", cost == 30, f"cost={cost} notes={notes}")
    check("交通费补算：给出可追溯的明细", len(notes) == 1 and "元" in notes[0], str(notes))

    plan_with_item = itinerary(
        [
            day(
                1,
                START,
                [
                    item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False),
                    item("打车去美术馆", "11:00", "11:20", 30, kind="transport"),
                    item("客家美术馆", "13:30", "15:30", 20, outdoor=False),
                ],
            )
        ]
    )
    engine.state = {"tool_results_cache": {}, "trace_log": []}
    cost2, notes2 = engine._leg_transport(plan_with_item)
    check("交通费补算：已有收费交通条目不重复累加", cost2 == 0 and not notes2, f"cost={cost2} notes={notes2}")

    engine.state = {"tool_results_cache": {}, "trace_log": []}
    walking_plan = itinerary(
        [
            day(
                1,
                START,
                [
                    item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False),
                    {**item("客家美术馆", "13:30", "15:30", 20, outdoor=False), "transport": "walking"},
                ],
            )
        ]
    )
    cost3, _ = engine._leg_transport(walking_plan)
    check("交通费补算：步行段不计费", cost3 == 0, f"cost={cost3}")

    over = validator.validate(
        itinerary([day(1, START, [item("中国客家博物馆", "09:00", "11:00", 0, outdoor=False)])]),
        constraints(budget=100, days=1, people=1),
        router=tools.execute_tool,
        extra_transport=500,
    )
    check("预算校验：点间交通费计入总预算", has_issue(over, "超过用户设定的"), str(over)[:160])


def test_truncated_json_fragment() -> None:
    """回归：输出被截断时，不能把某个「天」的片段当成整份行程。"""
    full = as_text(GOOD_PLAN)
    ok, err = schemas.parse_itinerary(full)
    check("解析：完整行程正常解析", ok is not None and len(ok.get("days") or []) == 2, str(err))

    truncated = full[: full.index('"days"') + 260]
    parsed, err = schemas.parse_itinerary(truncated)
    check("解析：截断输出不再被误判为合法行程", parsed is None, f"parsed={str(parsed)[:80]}")
    check("解析：截断时给出可操作提示", err is not None and "days" in err, str(err))
    check("解析：extract_json 仍然兼容非行程 JSON", (schemas.extract_json('{"a": 1}') or {}).get("a") == 1, "")


def test_split_opening_hours() -> None:
    """回归：分时段营业（午休闭馆）不能只按第一段判断。"""
    windows = validator.opening_windows("10:00-12:00 13:00-20:00")
    check("营业时间：午休分时段解析出两段", windows == [(600, 720), (780, 1200)], str(windows))

    plan = itinerary(
        [
            day(
                1,
                START,
                [{**item("道外巴洛克博物馆", "14:30", "16:00", 0, open_time="10:00-12:00 13:00-20:00")}],
            )
        ]
    )
    issues = issues_of(plan)
    check(
        "营业时间：下午时段进入分时段景点不再误判",
        not has_issue(issues, "不在开放时段内"),
        str(issues)[:160],
    )

    plan2 = itinerary(
        [
            day(
                1,
                START,
                [{**item("道外巴洛克博物馆", "12:30", "14:00", 0, open_time="10:00-12:00 13:00-20:00")}],
            )
        ]
    )
    issues2 = issues_of(plan2)
    check(
        "营业时间：午休时段仍会被拦下",
        has_issue(issues2, "不在开放时段内"),
        str(issues2)[:160],
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 配置：本地 .env 与云端 Streamlit Secrets 的取值顺序
# --------------------------------------------------------------------------- #
def test_config_secrets() -> None:
    # 没有 secrets.toml 时必须安静返回字符串（不能抛异常，否则 cli / evaluate 会崩）
    value = config._from_streamlit_secrets("AMAP_KEY")
    check("配置：Secrets 缺文件/缺键时不抛异常", isinstance(value, str), repr(value))

    probe = "SELFTEST_PROBE_ONLY"
    os.environ[probe] = "来自环境变量"
    try:
        check("配置：环境变量优先于 Secrets 与默认值", config._cfg(probe, "默认值") == "来自环境变量")
    finally:
        os.environ.pop(probe, None)

    # CLI / 评测环境没有 streamlit、也没有 secrets.toml 时，必须判定为"不在 Streamlit 里"，
    # 否则会为了取一个空 Secrets 白导入 streamlit。selftest 里前面的 AppTest 用例已经把
    # streamlit 载入 sys.modules，这里临时摘掉再还原，模拟真实的脚本环境。
    secrets_files = (
        Path.home() / ".streamlit" / "secrets.toml",
        config.BASE_DIR / ".streamlit" / "secrets.toml",
    )
    st_module = sys.modules.pop("streamlit", None)
    try:
        if not any(p.exists() for p in secrets_files):
            check("配置：非 Streamlit 环境不判定为线上", config._under_streamlit() is False)
            check("配置：非 Streamlit 环境返回值恒为空串", config._from_streamlit_secrets("AMAP_KEY") == "")
    finally:
        if st_module is not None:
            sys.modules["streamlit"] = st_module


def main() -> int:
    install_fakes()
    tests = (
        test_budget,
        test_weather_rule,
        test_duration_rule,
        test_dwell_target_advice,
        test_budget_rule,
        test_opening_rule,
        test_companion_rule,
        test_duplicate_rule,
        test_plan_level,
        test_parsing,
        test_coord_guard,
        test_markdown,
        test_route_v5_parsing,
        test_leg_transport,
        test_truncated_json_fragment,
        test_split_opening_hours,
        test_agent_self_correction,
        test_agent_max_iterations,
        test_ui_apptest,
        test_config_secrets,
    )
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - 单个用例失败不影响其它用例
            check(f"{test.__name__} 抛出异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for label, ok, detail in RESULTS:
        print(f"{'[OK]' if ok else '[FAIL]'} {label}" + (f" | {detail}" if not ok and detail else ""))
    print("-" * 68)
    print(f"自检结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
