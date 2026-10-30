"""核心 Agent 循环：理解需求 -> 多轮调工具 -> 生成行程 -> 代码校验 -> 自动重排 -> 交付。"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from datetime import date, timedelta
from typing import Any, Callable

from . import config, llm, schemas, tools, validator, zh_labels

#: 技术明细（原始参数、坐标、耗时）只进后台日志文件，不上屏。
_detail_logger = logging.getLogger("travel_agent.detail")

SYSTEM_PROMPT = """你是专业的国内旅行规划师，严格遵守以下规则：
1. 所有事实数据（景点、天气、路程、价格）必须调用工具获取，禁止编造任何信息。
2. 严格按照用户的约束规划：天数、预算、同行人、特殊需求（如避雨、少走路、老人同行等）。
3. 规划时按天拆分，每天不超过3个主要景点，景点间路程+游览时间总和不超过当日可用时长（默认8小时，早9到晚6）。
4. 优先把户外景点排到晴天，室内景点可以排雨天。
5. 当工具返回信息不足时，继续调用工具查询，不要猜测。
6. 当收到校验不通过的反馈时，根据问题重新调整行程，不要重复犯相同错误。
7. 最终输出结构化的行程单，包含每日时间安排、景点、交通方式、耗时、花费、注意事项。
8. 禁止自己计算距离、耗时、预算，一律以工具返回为准。
9. 高德接口坐标必须是 GCJ-02，且经纬度顺序为【经度在前、纬度在后】。"""

OUTPUT_SPEC = """最终回复只允许包含一个 ```json 代码块，结构如下（字段名不得改动、禁止多余文字）：
{
  "city": "梅州市",
  "summary": "一句话说明行程思路",
  "days": [
    {
      "day": 1,
      "date": "2026-11-02",
      "weather_note": "当天天气与安排理由",
      "items": [
        {
          "start": "09:00",
          "end": "11:00",
          "type": "attraction",
          "name": "雁南飞茶田",
          "lng": 116.407,
          "lat": 24.276,
          "cost": 60,
          "indoor": false,
          "open_time": "08:00-18:00",
          "transport": "driving",
          "walk_km": 1.5,
          "note": "备注"
        }
      ]
    }
  ],
  "tips": ["注意事项1"]
}
字段硬性要求：
- type 只能是 attraction / food / hotel / transport / rest；只有 attraction 参与可行性校验。
- lng / lat 必须是 geocode 或 search_poi 返回的真实坐标（GCJ-02），经度在前；禁止编造。
- cost 取 POI 的 cost 字段（未知填 0），open_time 取 POI 的 open_time（未返回填空字符串）。
- indoor：博物馆/展馆/纪念馆等室内为 true，公园/山地/街区/茶田等户外为 false。
- 每天时间在 09:00-18:00 之间，最多 3 个 attraction，相邻景点之间必须留出交通时间（按 plan_route 结果留足，宁可多留 10-15 分钟）。
- 单日时间预算（先算再排，别排完才发现超时）：游览合计 ≤ 5 小时（带老人/小孩 ≤ 4 小时）；
  排 3 个景点就每个排 1.5 小时（合计 4.5 小时），单个景点不要超过 2 小时；
  午休 1 小时 + 市内交通约 1.5 小时也占这 8 小时，排不下就果断少排一个景点——
  不要靠把单个景点压到 1 小时以下来硬凑，那样校验仍会判超时。
- transport 填到达该景点所用的 driving / walking / transit。
- walk_km 是**该景点园区内部的步行距离**（通常 0.5-2 公里/景点），不是景点之间的路程；带老人/小孩时单日合计不要超过 5 公里。
- note 不超过 40 字、weather_note 不超过 30 字（控制输出长度，避免 JSON 被截断）。
- date 必须是 YYYY-MM-DD。"""


class AgentError(RuntimeError):
    """面向用户的、可直接展示的错误。"""


def build_constraints(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """把界面/命令行输入整理成结构化用户约束（含默认值）。"""
    raw = raw or {}
    city = str(raw.get("city") or "").strip()
    if not city:
        raise AgentError("请先填写目的地城市")
    days = int(raw.get("days") or 1)
    if not 1 <= days <= 10:
        raise AgentError("行程天数需在 1-10 天之间")
    people = max(1, int(raw.get("people") or 1))
    budget = float(raw.get("budget") or 0) or 3000.0
    start = raw.get("start_date")
    if isinstance(start, str):
        try:
            start = date.fromisoformat(start.strip())
        except ValueError:
            start = date.today() + timedelta(days=1)
    if not isinstance(start, date):
        start = date.today() + timedelta(days=1)
    companions = raw.get("companions") or []
    if isinstance(companions, str):
        companions = [c.strip() for c in companions.replace("，", ",").split(",") if c.strip()]
    return {
        "city": city,
        "days": days,
        "people": people,
        "rooms": max(1, int(raw.get("rooms") or (people + 1) // 2)),
        "budget": budget,
        "companions": list(companions),
        "preferences": str(raw.get("preferences") or "").strip(),
        "start_date": start.isoformat(),
        "daily_hours": config.DAILY_HOURS,
        "food_per_person_per_day": float(raw.get("food_per_person_per_day") or config.FOOD_PER_PERSON_PER_DAY),
        "hotel_per_night": float(raw.get("hotel_per_night") or config.HOTEL_PER_NIGHT),
        "request": str(raw.get("request") or "").strip(),
    }


def constraint_lines(c: dict[str, Any]) -> str:
    start = date.fromisoformat(c["start_date"])
    dates = [f"第{i + 1}天 {(start + timedelta(days=i)).isoformat()}" for i in range(c["days"])]
    companions = "/".join(c["companions"]) or "无"
    return (
        f"- 目的地城市：{c['city']}\n"
        f"- 行程天数：{c['days']} 天（{'；'.join(dates)}）\n"
        f"- 出行人数：{c['people']} 人，房间：{c['rooms']} 间\n"
        f"- 总预算上限：{c['budget']:.0f} 元（餐饮 {c['food_per_person_per_day']:.0f} 元/人/天，"
        f"住宿 {c['hotel_per_night']:.0f} 元/间/晚）\n"
        f"- 同行人：{companions}\n"
        f"- 特殊偏好：{c['preferences'] or '无'}\n"
        f"- 每日可用时长：{c['daily_hours']:.0f} 小时（09:00-18:00）"
    )


class TravelAgent:
    """带自纠错闭环的旅行规划 Agent（手写循环，不依赖任何 Agent 框架）。"""

    def __init__(
        self,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
        chat: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = llm.chat,
    ) -> None:
        self.trace_callback = trace_callback
        self._chat = chat
        self.state: dict[str, Any] = {}
        self.round_no = 0

    # ------------------------------------------------------------------ #
    # 状态 / Trace
    # ------------------------------------------------------------------ #
    def _rp(self) -> str:
        return zh_labels.round_label(self.round_no)

    def _trace(self, line: str, kind: str = "info", **extra: Any) -> None:
        entry = {"round": self.round_no, "kind": kind, "line": line, **extra}
        self.state.setdefault("trace_log", []).append(entry)
        if self.trace_callback:
            try:
                self.trace_callback(entry)
            except Exception:  # noqa: BLE001 - 回调异常不影响主流程
                pass

    def _flush_cache_summary(self) -> None:
        """缓存命中不逐条刷屏，每轮汇总一句。"""
        pending = int(self.state.pop("cache_hits_pending", 0) or 0)
        if pending:
            self._trace(
                f"{self._rp()}本轮命中缓存 {pending} 次，节省 {pending} 次接口调用",
                "cache",
                cache_hits=pending,
            )

    @staticmethod
    def _log_detail(kind: str, payload: dict[str, Any]) -> None:
        """原始参数 / 坐标 / 毫秒耗时写后台日志（页面看不到）。"""
        if _detail_logger.handlers or _detail_logger.propagate:
            try:
                _detail_logger.info("%s %s", kind, json.dumps(payload, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001 - 日志失败不影响主流程
                pass

    def _remember_places(self, result: dict[str, Any]) -> None:
        """记住坐标 -> 名称，日志里才能写「A → B」而不是一串经纬度。"""
        if not result.get("success"):
            return
        names: dict[tuple[float, float], str] = self.state.setdefault("place_names", {})
        points = [result] if result.get("name") and result.get("lng") is not None else []
        points += [poi for poi in (result.get("pois") or []) if poi.get("name")]
        for point in points:
            try:
                key = (round(float(point["lng"]), 4), round(float(point["lat"]), 4))
            except (KeyError, TypeError, ValueError):
                continue
            names[key] = str(point["name"])

    def _place_name(self, lng: Any, lat: Any) -> str:
        try:
            key = (round(float(lng), 4), round(float(lat), 4))
        except (TypeError, ValueError):
            return ""
        name = (self.state.get("place_names") or {}).get(key)
        if name:
            return str(name)
        # 兜底：校验阶段查路时，坐标来自模型已排好的行程草稿
        for day in (self.state.get("itinerary_draft") or {}).get("days") or []:
            for item in day.get("items") or []:
                try:
                    if (round(float(item.get("lng")), 4), round(float(item.get("lat")), 4)) == key:
                        return str(item.get("name") or "")
                except (TypeError, ValueError):
                    continue
        return ""


    # ------------------------------------------------------------------ #
    # 工具执行（参数校验 + 缓存 + Trace）
    # ------------------------------------------------------------------ #
    def _city(self) -> str:
        return str((self.state.get("user_constraints") or {}).get("city") or "")

    def _describe_call(self, name: str, args: dict[str, Any]) -> str:
        return zh_labels.describe_tool_call(
            name,
            args,
            city=self._city(),
            origin=self._place_name(args.get("origin_lng"), args.get("origin_lat")),
            dest=self._place_name(args.get("dest_lng"), args.get("dest_lat")),
        )

    def _execute(self, name: str, raw_args: Any, source: str = "model") -> dict[str, Any]:
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as exc:
                result = {"success": False, "error": f"参数不是合法 JSON：{exc}"}
                self._trace(
                    f"{self._rp()}参数格式有误，已跳过这次工具调用",
                    "tool_call",
                    tool=name,
                    args_ok=False,
                    detail=str(exc),
                )
                return result
        if not isinstance(raw_args, dict):
            raw_args = {}
        valid, err = schemas.validate_args(name, raw_args)
        if valid is None:
            result = {"success": False, "error": err or "参数非法"}
            self._trace(
                f"{self._rp()}参数不符合要求，已跳过这次工具调用",
                "tool_call",
                tool=name,
                args_ok=False,
                detail=str(err),
            )
            self._log_detail("tool_call_invalid", {"tool": name, "args": raw_args, "error": err})
            return result

        cache: dict[str, Any] = self.state.setdefault("tool_results_cache", {})
        cache_key = f"{name}:{json.dumps(valid, sort_keys=True, ensure_ascii=False)}"
        if name != "estimate_budget" and cache_key in cache:
            # 缓存命中不逐条打印，每轮汇总一句（见 _flush_cache_summary）
            self.state["cache_hits"] = self.state.get("cache_hits", 0) + 1
            self.state["cache_hits_pending"] = self.state.get("cache_hits_pending", 0) + 1
            self._log_detail("cache_hit", {"tool": name, "args": valid})
            return cache[cache_key]

        self._trace(
            f"{self._rp()}{self._describe_call(name, valid)}",
            "tool_call",
            tool=name,
            args=valid,
            args_ok=True,
        )
        self._log_detail("tool_call", {"tool": name, "args": valid, "source": source})
        started = time.time()
        result = tools.execute_tool(name, valid, draft=self.state.get("itinerary_draft"))
        elapsed_ms = int((time.time() - started) * 1000)
        if name != "estimate_budget":
            cache[cache_key] = result
        self.state["tool_call_count"] = self.state.get("tool_call_count", 0) + 1
        self._remember_places(result)
        self._trace(
            f"{self._rp()}{zh_labels.describe_tool_result(name, result)}",
            "tool_result",
            tool=name,
            args_ok=result.get("success", False),
            elapsed_ms=elapsed_ms,
        )
        self._log_detail(
            "tool_result",
            {"tool": name, "args": valid, "elapsed_ms": elapsed_ms, "result": result},
        )
        return result

    def _validate_router(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """校验层调用的工具走同一条通路（复用缓存，Trace 可追溯）。"""
        return self._execute(name, args, source="validator")

    # ------------------------------------------------------------------ #
    # 点间交通费：模型只写「怎么去」，费用由代码按 plan_route 结果累加
    # ------------------------------------------------------------------ #
    @staticmethod
    def _num(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _cached_route(self, mode: str, origin: tuple[float, float], dest: tuple[float, float]) -> dict[str, Any] | None:
        """在工具结果缓存里找同一段路（坐标容差 1e-4 ≈ 11 米），命中就不重复请求。"""
        cache = self.state.get("tool_results_cache") or {}
        for key, value in cache.items():
            if not str(key).startswith("plan_route:"):
                continue
            try:
                args = json.loads(str(key).split(":", 1)[1])
            except (json.JSONDecodeError, IndexError):
                continue
            if args.get("mode") != mode:
                continue
            pairs = (
                (self._num(args.get("origin_lng")), origin[0]),
                (self._num(args.get("origin_lat")), origin[1]),
                (self._num(args.get("dest_lng")), dest[0]),
                (self._num(args.get("dest_lat")), dest[1]),
            )
            if all(a is not None and b is not None and abs(a - b) < 1e-4 for a, b in pairs):
                return value
        return None

    def _leg_transport(self, itinerary: dict[str, Any]) -> tuple[float, list[str]]:
        """汇总行程内相邻停靠点之间的用车费用，返回 (总额, 明细)。

        只在行程没有自带收费交通条目时启用，避免与模型的 transport 条目重复计算。
        """
        for day in itinerary.get("days") or []:
            for item in day.get("items") or []:
                if item.get("type") == "transport" and (self._num(item.get("cost")) or 0) > 0:
                    return 0.0, []

        total = 0.0
        notes: list[str] = []
        tried = 0
        for day in itinerary.get("days") or []:
            stops = [
                item
                for item in (day.get("items") or [])
                if item.get("type") in ("attraction", "food", "hotel")
                and self._num(item.get("lng")) is not None
                and self._num(item.get("lat")) is not None
            ]
            for prev, cur in zip(stops, stops[1:]):
                mode = cur.get("transport")
                mode = mode if mode in ("driving", "walking", "transit") else "driving"
                if mode == "walking":
                    continue
                origin = (self._num(prev.get("lng")), self._num(prev.get("lat")))
                dest = (self._num(cur.get("lng")), self._num(cur.get("lat")))
                route = self._cached_route(mode, origin, dest)
                if route is None:
                    if tried >= 10:  # 兜底：避免极端行程把接口打爆
                        continue
                    tried += 1
                    route = self._validate_router(
                        "plan_route",
                        {
                            "origin_lng": origin[0],
                            "origin_lat": origin[1],
                            "dest_lng": dest[0],
                            "dest_lat": dest[1],
                            "mode": mode,
                        },
                    )
                if not route.get("success"):
                    continue
                cost = self._num(route.get("cost_yuan")) or 0.0
                if cost <= 0:
                    continue
                total += cost
                notes.append(f"{prev.get('name') or '?'}→{cur.get('name') or '?'} {cost:.0f} 元")
        return round(total, 1), notes

    @staticmethod
    def _summarize(name: str, result: dict[str, Any]) -> str:
        """工具返回的一句话结论（人话版，不含坐标；兼容旧调用）。"""
        return zh_labels.describe_tool_result(name, result)

    # ------------------------------------------------------------------ #
    # 提示词组装
    # ------------------------------------------------------------------ #
    def _user_prompt(self, request: str, c: dict[str, Any]) -> str:
        coords = ""
        if c.get("lng") is not None:
            coords = (
                f"\n城市中心坐标（已由 geocode 工具返回，GCJ-02，经度在前）："
                f"lng={c['lng']}, lat={c['lat']}，可直接用于 get_weather。"
            )
        prefetched = self._prefetch_block(c)
        prefetch_part = ""
        if prefetched:
            prefetch_part = (
                "【已预取数据（代码在本轮对话开始前已调用工具查好，均为真实数据）】\n"
                f"{prefetched}\n"
                "以上天气与景点候选无需重复查询；仍需你调用 plan_route 查询所选景点之间的真实路程与耗时。\n\n"
            )
        return (
            "【用户需求】\n"
            f"{request or '按默认偏好规划'}\n\n"
            "【结构化约束】\n"
            f"{constraint_lines(c)}{coords}\n\n"
            f"{prefetch_part}"
            "【工作方式】\n"
            "1. 第一轮就并行发出所有需要的工具调用：相邻景点逐段的 plan_route（当天相邻景点每段都要查，不要漏），"
            "不要一个工具一轮慢慢试。\n"
            "2. 景点从上面的候选清单里挑（坐标、票价、营业时间直接抄，不要编造）；候选不够时才补充 search_poi。\n"
            "3. 信息足够就立即输出最终 JSON，不要为了“再看看”多调工具；校验轮次越少越好。\n"
            "4. 所有景点名称、坐标、票价、营业时间都必须来自工具或预取结果，禁止编造。\n"
            "5. 输出前先自查三件事：① 每个条目 end 必须晚于 start（住宿写“18:00-18:30 办理入住”，不要写 18:00-18:00）；"
            "② 单日「游览 + 交通 + 午休 1 小时」不超过当日可用时长——先加一遍游览时长，"
            "超过 5 小时（带老人 4 小时）就直接删掉最远的那个景点；③ 降水概率高的日子不排户外景点。\n"
            f"6. 最终按下面的模板输出行程 JSON（最多 {config.MAX_ATTRACTIONS_PER_DAY} 个景点/天，"
            f"每天总时长不超过 {config.DAILY_HOURS:.0f} 小时）：\n{OUTPUT_SPEC}"
        )

    def _call_llm(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            message, usage = self._chat(messages, tools=schemas.TOOLS)
        except llm.LLMError as exc:
            raise AgentError(str(exc)) from exc
        if usage:
            self.state["usage"] = usage
            if usage.get("finish_reason") == "length":
                self.state["truncated"] = True
                self._trace(f"{self._rp()}输出触达长度上限被截断，将要求模型精简后重发", "truncated")
        return message

    # ------------------------------------------------------------------ #
    # 城市中心坐标（代码调用工具，供天气校验与模型使用）
    # ------------------------------------------------------------------ #
    def _seed_city(self, c: dict[str, Any]) -> None:
        self.round_no = 0
        result = self._execute("geocode", {"place_name": c["city"]}, source="prepare")
        if result.get("success"):
            c["lng"], c["lat"] = result["lng"], result["lat"]
            c["city_full"] = result.get("city") or c["city"]
            self._trace(
                f"{self._rp()}城市定位完成：{c['city_full']}（用于天气与路程计算）",
                "prepare",
                detail="坐标已记录到后台日志",
            )
            self._log_detail("city_center", {"city": c["city_full"], "lng": c["lng"], "lat": c["lat"]})
        else:
            self._trace(f"{self._rp()}城市定位失败，天气校验将跳过", "prepare")
            self._log_detail("city_center_failed", {"city": c["city"], "error": result.get("error")})

    # ------------------------------------------------------------------ #
    # 代码侧预取：进模型前把天气与景点候选查好（省掉 1-2 轮纯取数往返）
    # ------------------------------------------------------------------ #
    PREFETCH_KEYWORDS = (("景点", 8), ("公园", 6), ("博物馆", 6))

    def _prefetch_results(
        self, tasks: list[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
        """并行执行预取任务，结果写入工具缓存（模型后续重复调用直接命中，不再发请求）。"""
        prepared: list[tuple[str, dict[str, Any], str]] = []
        for name, raw in tasks:
            valid, err = schemas.validate_args(name, raw)
            if valid is None:
                self._trace(f"{self._rp()}预取参数不符合要求，已跳过", "tool_call", tool=name, args_ok=False)
                continue
            key = f"{name}:{json.dumps(valid, sort_keys=True, ensure_ascii=False)}"
            prepared.append((name, valid, key))
        if not prepared:
            return []

        cache: dict[str, Any] = self.state.setdefault("tool_results_cache", {})
        workers = min(4, len(prepared))
        done: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(tools.execute_tool, name, valid) for name, valid, _ in prepared]
            for (name, valid, key), future in zip(prepared, futures):
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 - 预取失败不阻断主流程
                    result = {"success": False, "error": f"预取失败：{exc}"}
                cache[key] = result
                self.state["tool_call_count"] = self.state.get("tool_call_count", 0) + 1
                self._remember_places(result)
                self._trace(
                    f"{self._rp()}预取：{self._describe_call(name, valid)}",
                    "tool_call",
                    tool=name,
                    args=valid,
                    args_ok=True,
                )
                self._trace(
                    f"{self._rp()}预取：{self._summarize(name, result)}",
                    "tool_result",
                    tool=name,
                    args_ok=result.get("success", False),
                )
                self._log_detail("prefetch", {"tool": name, "args": valid, "result": result})
                done.append((name, valid, result))
        return done

    def _prefetch(self, c: dict[str, Any]) -> None:
        """并行查好天气与景点候选；失败只降级（模型仍可自行调工具），不影响主流程。"""
        tasks: list[tuple[str, dict[str, Any]]] = []
        if c.get("lng") is not None and c.get("lat") is not None:
            tasks.append(
                (
                    "get_weather",
                    {"lng": c["lng"], "lat": c["lat"], "days": validator.weather_query_days(c)},
                )
            )
        tasks += [
            ("search_poi", {"city": c["city"], "keyword": kw, "count": n})
            for kw, n in self.PREFETCH_KEYWORDS
        ]
        started = time.time()
        weather: list[dict[str, Any]] = []
        pois: dict[str, list[dict[str, Any]]] = {}
        for name, args, result in self._prefetch_results(tasks):
            if not result.get("success"):
                continue
            if name == "get_weather":
                weather = result.get("days") or []
            elif name == "search_poi":
                pois[str(args.get("keyword"))] = (result.get("pois") or [])[:6]
        self.state["prefetch"] = {"weather": weather, "pois": pois}
        hits = sum(len(items) for items in pois.values())
        self._trace(
            f"{self._rp()}预取完成：天气 {len(weather)} 天、景点候选 {hits} 个",
            "prepare",
            detail=f"{len(pois)} 个关键词，耗时 {round(time.time() - started, 1)} 秒",
        )

    def _prefetch_block(self, c: dict[str, Any]) -> str:
        """把预取结果整理成提示词片段：直接给模型可用的候选清单。"""
        data = self.state.get("prefetch") or {}
        lines: list[str] = []

        weather = {day.get("date"): day for day in data.get("weather") or [] if day.get("date")}
        start = date.fromisoformat(c["start_date"])
        for index in range(c["days"]):
            key = (start + timedelta(days=index)).isoformat()
            day = weather.get(key)
            if not day:
                continue
            flag = "适宜户外" if day.get("is_outdoor_ok") else "不宜户外（优先排室内）"
            lines.append(
                f"- 第{index + 1}天 {key}：降水概率 {day.get('rain_prob')}%，"
                f"{day.get('min_temp')}~{day.get('max_temp')}℃，{flag}"
            )
        if lines:
            lines.insert(0, "天气（已按行程日期查好，数据真实，不要再调用 get_weather）：")

        pois = data.get("pois") or {}
        if pois:
            lines.append("景点候选（search_poi 真实返回；GCJ-02 坐标，经度在前；已标注室内/户外）：")
            for keyword, items in pois.items():
                lines.append(f"「{keyword}」：")
                for poi in items:
                    cost = poi.get("cost")
                    cost_text = f"{cost:.0f} 元" if isinstance(cost, (int, float)) else "票价未知"
                    lines.append(
                        f"  · {poi.get('name')} | lng={poi.get('lng')}, lat={poi.get('lat')} | "
                        f"{cost_text} | {poi.get('open_time') or '营业时间未知'} | "
                        f"{'室内' if validator.is_indoor(poi) else '户外'}"
                    )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self, request: str, constraints: dict[str, Any]) -> dict[str, Any]:
        """一次完整规划：多轮工具调用 + 校验重排，最多 config.MAX_ITERATIONS 轮。"""
        started = time.time()
        payload = dict(constraints)
        payload["request"] = request or payload.get("request") or ""
        c = build_constraints(payload)
        self.state = {
            "user_constraints": c,
            "tool_results_cache": {},
            "itinerary_draft": None,
            "trace_log": [],
        }
        self.round_no = 0
        self._trace(
            f"{self._rp()}收到需求：{c['city']} {c['days']} 天 {c['people']} 人，预算 {c['budget']:.0f} 元，"
            f"同行人 {('、'.join(c['companions']) or '无')}",
            "constraint",
        )
        self._seed_city(c)
        self._prefetch(c)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_prompt(c["request"], c)},
        ]
        itinerary: dict[str, Any] | None = None
        issues: list[str] = []
        iterations = 0
        issue_signature: tuple[str, ...] | None = None
        same_streak = 0

        for round_no in range(1, config.MAX_ITERATIONS + 1):
            iterations = round_no
            self.round_no = round_no
            message = self._call_llm(messages)
            tool_calls = message.get("tool_calls") or []

            if tool_calls:
                messages.append(
                    {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}
                )
                names = [zh_labels.tool_name((call.get("function") or {}).get("name")) for call in tool_calls]
                self._trace(
                    f"{self._rp()}模型决策：调用 {len(tool_calls)} 个工具（{'、'.join(names)}）",
                    "decision",
                )
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name") or ""
                    result = self._execute(name, fn.get("arguments"), source="model")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "name": name,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                self._flush_cache_summary()
                continue

            text = message.get("content") or ""
            parsed, err = schemas.parse_itinerary(text)
            if parsed is None:
                truncated = self.state.pop("truncated", False)
                self._trace(f"{self._rp()}行程草稿没能按格式解析，已请模型重发", "parse_error", detail=str(err))
                messages.append({"role": "assistant", "content": text})
                extra = (
                    "上一次回复因为达到输出长度上限被截断。请把每个条目的 note / weather_note 压到 30 字以内，"
                    "只保留必要字段，确保整份 JSON 完整闭合。"
                    if truncated
                    else "请只输出一个 ```json 代码块，字段严格按模板，不要输出任何额外文字。"
                )
                messages.append(
                    {
                        "role": "user",
                        "content": f"上面的输出无法解析为结构化行程（{err}）。{extra}",
                    }
                )
                continue
            self.state.pop("truncated", None)

            self.state["itinerary_draft"] = parsed
            leg_cost, leg_notes = self._leg_transport(parsed)
            issues = validator.validate(parsed, c, router=self._validate_router, extra_transport=leg_cost)
            if issues:
                self._flush_cache_summary()
                self._trace(f"{self._rp()}校验结果：发现 {len(issues)} 个问题，需要重排", "validation")
                for idx, issue in enumerate(issues[:5], 1):
                    self._trace(f"{self._rp()}问题 {idx}：{zh_labels.short_issue(issue)}", "issue", detail=issue)
                if len(issues) > 5:
                    self._trace(
                        f"{self._rp()}另有 {len(issues) - 5} 个问题，已一并反馈给模型",
                        "issue",
                    )
                signature = tuple(sorted(issues))
                if signature == issue_signature:
                    same_streak += 1
                else:
                    issue_signature, same_streak = signature, 0
                if same_streak >= 2:
                    self._trace(
                        f"{self._rp()}同一组问题已连续 {same_streak + 1} 轮未改善，"
                        "停止重排以免空转到上限（节省时间与接口费用）",
                        "give_up",
                    )
                    break
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": "行程有以下问题，请重排（只修改有问题的地方，所有景点/坐标/价格仍必须来自工具结果，"
                        "不要重复犯同样的错误）：\n"
                        + "\n".join(f"{i}. {x}" for i, x in enumerate(issues, 1))
                        + "\n请重新输出完整行程 JSON。",
                    }
                )
                continue

            self._flush_cache_summary()
            self._trace(f"{self._rp()}校验结果：6 项规则全部通过", "validation")
            if leg_cost > 0:
                self._trace(
                    f"{self._rp()}点间交通费：{'、'.join(leg_notes)}，合计 {leg_cost:.0f} 元"
                    "（由路线规划结果按代码累加）",
                    "budget",
                )
            self.state["leg_transport"] = (leg_cost, leg_notes)
            itinerary = parsed
            break

        if itinerary is None:
            itinerary = self.state.get("itinerary_draft") or {}
        status = "ok" if (itinerary and not issues) else ("max_iterations" if itinerary else "failed")

        budget = tools.estimate_budget(
            itinerary,
            days=c["days"],
            people=c["people"],
            rooms=c["rooms"],
            food_per_person_per_day=c["food_per_person_per_day"],
            hotel_per_night=c["hotel_per_night"],
        )
        leg_cost, leg_notes = self.state.get("leg_transport") or (0.0, [])
        if leg_cost > 0 and budget.get("success"):
            breakdown = budget.get("breakdown") or {}
            breakdown["transport"] = round((breakdown.get("transport") or 0) + leg_cost, 1)
            budget["breakdown"] = breakdown
            budget["total"] = round((budget.get("total") or 0) + leg_cost, 1)
            budget["route_note"] = f"点间交通费 {leg_cost:.0f} 元由路线规划结果按代码累加"
        elapsed = round(time.time() - started, 1)
        tool_calls = self.state.get("tool_call_count", 0)
        cache_hits = self.state.get("cache_hits", 0)
        self._trace(
            f"【完成】状态：{zh_labels.status(status)}｜迭代 {iterations} 次｜工具调用 {tool_calls} 次｜"
            f"耗时 {elapsed} 秒｜总预算 {budget.get('total')} 元",
            "done",
            iterations=iterations,
            tool_calls=tool_calls,
            cache_hits=cache_hits,
        )
        result: dict[str, Any] = {
            "status": status,
            "itinerary": itinerary,
            "constraints": c,
            "budget": budget,
            "issues": issues,
            "iterations": iterations,
            "tool_calls": tool_calls,
            "cache_hits": cache_hits,
            "elapsed": elapsed,
            "trace": self.state["trace_log"],
            "usage": self.state.get("usage", {}),
        }
        result["markdown"] = render_markdown(result)
        return result


# --------------------------------------------------------------------------- #
# 交付层：Markdown 行程单
# --------------------------------------------------------------------------- #
TYPE_LABELS = zh_labels.ITEM_TYPE_LABELS
TRANSPORT_LABELS = zh_labels.TRANSPORT_LABELS


def render_markdown(result: dict[str, Any]) -> str:
    """把结构化行程渲染成可直接导出的 Markdown 行程单。"""
    c = result.get("constraints") or {}
    itinerary = result.get("itinerary") or {}
    budget = result.get("budget") or {}
    days = itinerary.get("days") or []

    lines = [f"# {itinerary.get('city') or c.get('city', '')} {c.get('days', len(days))} 天行程单", ""]
    if itinerary.get("summary"):
        lines += [f"> {itinerary['summary']}", ""]
    lines.append(
        f"- 出发日期：{c.get('start_date', '-')}　行程天数：{len(days)} 天　人数：{c.get('people', 1)} 人"
    )
    lines.append(
        f"- 同行人：{'、'.join(c.get('companions') or []) or '无'}　特殊偏好：{c.get('preferences') or '无'}"
    )
    lines.append(
        f"- 总预算：**{budget.get('total', 0)} 元**（用户上限 {float(c.get('budget') or 0):.0f} 元）"
    )
    lines.append(
        f"- 生成状态：{zh_labels.status(result.get('status'))}　迭代：{result.get('iterations')} 轮　"
        f"工具调用：{result.get('tool_calls')} 次　耗时：{result.get('elapsed')} 秒"
    )
    lines.append("")

    parts = budget.get("breakdown") or {}
    lines += ["## 预算明细", "", "| 项目 | 金额（元） |", "| --- | --- |"]
    for key, value in parts.items():
        lines.append(f"| {zh_labels.budget_item(key)} | {value} |")
    lines += [f"| **合计** | **{budget.get('total', 0)}** |", ""]
    if budget.get("route_note"):
        lines += [f"> {budget['route_note']}", ""]

    for index, day in enumerate(days):
        lines += [f"## 第{day.get('day') or index + 1}天 {day.get('date') or ''}", ""]
        if day.get("weather_note"):
            lines += [f"天气：{day['weather_note']}", ""]
        lines += [
            "| 时间 | 类型 | 安排 | 交通 | 花费（元） | 备注 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in day.get("items") or []:
            note = item.get("note") or ""
            if item.get("open_time"):
                note = f"营业 {item['open_time']}　{note}".strip()
            lines.append(
                "| {time} | {type} | {name} | {transport} | {cost} | {note} |".format(
                    time=f"{item.get('start', '')}-{item.get('end', '')}",
                    type=zh_labels.item_type(item.get("type")),
                    name=item.get("name", ""),
                    transport=zh_labels.transport(item.get("transport")),
                    cost=item.get("cost", 0),
                    note=note.replace("|", "/"),
                )
            )
        lines.append("")

    tips = itinerary.get("tips") or []
    if tips:
        lines += ["## 注意事项", ""] + [f"- {tip}" for tip in tips] + [""]

    issues = result.get("issues") or []
    if issues:
        lines += ["## 未解决的校验问题（已达迭代上限）", ""] + [f"- {issue}" for issue in issues] + [""]

    key_lines = [
        str(entry.get("line", ""))
        for entry in result.get("trace") or []
        if zh_labels.is_key_trace(entry) and entry.get("line")
    ]
    tech_lines = [
        zh_labels.describe_technical(entry)
        for entry in result.get("trace") or []
        if zh_labels.is_tech_trace(entry)
    ]
    lines += ["## 执行过程", "", "```text"]
    lines += key_lines[-30:]
    lines += ["```", "", "<details><summary>技术明细（每次工具调用的参数与耗时）</summary>", "", "```text"]
    lines += tech_lines
    lines += [
        "```",
        "",
        "</details>",
        "",
        "---",
        "",
        "数据来源：DeepSeek（决策）· 高德地图（地理编码 / 地点检索 / 路径规划）· "
        "Open-Meteo（天气）· 预算与耗时由代码计算。",
    ]
    return "\n".join(lines)
