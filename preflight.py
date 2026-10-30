"""上线前预检：确认 .env 里的两个 Key 真实可用，并验证 strict 工具模式是否生效。

用法：
    python preflight.py          # 只做连通性检查：约 5-10 秒，几乎不消耗 token
    python preflight.py --full   # 额外跑一次完整规划：约 20-40 秒，会消耗少量 token

退出码：0 全部通过 / 1 有检查未通过 / 2 缺少 Key
"""
from __future__ import annotations

import sys
import time
from typing import Any

from travel_agent import agent, config, llm, schemas, tools

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((label, bool(ok), detail))
    suffix = f" | {detail}" if detail and not ok else ""
    print(f"{'[OK]' if ok else '[FAIL]'} {label}{suffix}")
    return bool(ok)


def masked(value: str) -> str:
    if not value:
        return "（空）"
    return value[:4] + "*" * max(0, len(value) - 8) + value[-4:] if len(value) > 8 else "已填写"


# --------------------------------------------------------------------------- #
# 1. Key 是否填写
# --------------------------------------------------------------------------- #
def check_keys() -> bool:
    missing = config.require_keys()
    print(f"  模型：{config.DEEPSEEK_MODEL}　端点：{config.DEEPSEEK_BASE_URL}")
    print(f"  DEEPSEEK_API_KEY={masked(config.DEEPSEEK_API_KEY)}　AMAP_KEY={masked(config.AMAP_KEY)}")
    return check("两个 Key 都已填写", not missing, "缺少：" + "、".join(missing))


# --------------------------------------------------------------------------- #
# 2. 高德：地理编码 + POI 搜索
# --------------------------------------------------------------------------- #
def check_amap() -> tuple[float, float] | None:
    started = time.time()
    geo = tools.geocode("梅州市")
    if not check("高德地理编码可用", geo.get("success") is True, str(geo.get("error", ""))):
        error = str(geo.get("error", "")).upper()
        if "KEY" in error or "INVALID" in error or "QUOTA" in error or "AMAP" in error:
            print("  提示：Key 类型必须是「Web服务」；检查是否复制完整、是否已开通额度。")
        return None
    lng, lat = float(geo["lng"]), float(geo["lat"])
    print(f"  梅州市 -> lng={lng} lat={lat} city={geo.get('city')}（{time.time() - started:.1f}s）")

    poi = tools.search_poi("梅州市", "客家", 3)
    if check("高德 POI 搜索可用", poi.get("success") is True, str(poi.get("error", ""))):
        names = [item["name"] for item in poi.get("pois", [])][:3]
        print(f"  关键词「客家」命中：{'、'.join(names)}")
    return lng, lat


# --------------------------------------------------------------------------- #
# 3. Open-Meteo 天气（无需 Key）
# --------------------------------------------------------------------------- #
def check_weather(lng: float, lat: float) -> None:
    weather = tools.get_weather(lng, lat, 3)
    ok = check("天气接口可用（Open-Meteo）", weather.get("success") is True, str(weather.get("error", "")))
    if ok:
        for day in weather.get("days", [])[:3]:
            print(
                f"  {day['date']} 降水概率 {day['rain_prob']}% "
                f"{day['min_temp']}~{day['max_temp']}℃ 户外{'可行' if day['is_outdoor_ok'] else '不宜'}"
            )


# --------------------------------------------------------------------------- #
# 4. 高德路径规划（预算与耗时校验都依赖它）
# --------------------------------------------------------------------------- #
def check_route(lng: float, lat: float) -> None:
    route = tools.plan_route(lng, lat, lng + 0.05, lat + 0.03, "driving")
    ok = check("路径规划可用（driving）", route.get("success") is True, str(route.get("error", "")))
    if ok:
        print(
            f"  距离 {route.get('distance_m')} 米　耗时 {route.get('duration_sec')} 秒　"
            f"费用 {route.get('cost_yuan')} 元　估算={route.get('estimated')}"
        )


# --------------------------------------------------------------------------- #
# 5. DeepSeek：连通性 + strict Schema 是否被接受 + 是否会产出工具调用
# --------------------------------------------------------------------------- #
def check_llm() -> None:
    started = time.time()
    messages = [
        {"role": "system", "content": "你是旅行规划助手。需要外部数据时必须调用工具，不要编造。"},
        {"role": "user", "content": "请调用 geocode 工具查询「梅州市」的坐标，不要直接回答。"},
    ]
    try:
        message, usage = llm.chat(messages, tools=schemas.TOOLS, max_tokens=256)
    except llm.LLMError as exc:
        check("DeepSeek 调用可用", False, str(exc))
        return
    check("DeepSeek 调用可用", bool(message), "返回为空")
    print(f"  耗时 {time.time() - started:.1f}s　tokens={usage.get('total_tokens', '?')}")

    if llm.STRICT_STATUS["downgraded"]:
        check(
            "strict 工具模式生效",
            False,
            f"账号不支持 strict，已自动降级为普通模式：{llm.STRICT_STATUS['reason'][:120]}",
        )
        print("  说明：功能不受影响，但答辩时请说「支持 strict，账号不支持时自动降级」。")
    else:
        check("strict 工具模式生效", config.STRICT_TOOLS, "STRICT_TOOLS=0，未启用 strict")

    calls = message.get("tool_calls") or []
    if check("模型会主动调用工具", bool(calls), "模型只回文本、没有调用工具"):
        function = calls[0].get("function", {})
        print(f"  工具调用：{function.get('name')}({function.get('arguments')})")


# --------------------------------------------------------------------------- #
# 6. 可选：完整跑一次规划（会消耗 token）
# --------------------------------------------------------------------------- #
def check_full_run() -> None:
    started = time.time()
    print("\n  跑一次完整规划：梅州市 1 天 / 2 人 / 预算 1200 ……")
    engine = agent.TravelAgent(trace_callback=lambda entry: print(f"    · {entry['line']}"))
    try:
        result = engine.run(
            "想看看客家文化，节奏轻松一点",
            {
                "city": "梅州市",
                "days": 1,
                "people": 2,
                "budget": 1200,
                "start_date": "",
                "companions": "",
                "preferences": "",
            },
        )
    except agent.AgentError as exc:
        check("完整规划跑通", False, str(exc))
        return
    check(
        "完整规划跑通",
        result["status"] == "ok",
        f"status={result['status']} 遗留问题={len(result['issues'])} 条",
    )
    print(
        f"  状态 {result['status']}　迭代 {result['iterations']}　工具 {result['tool_calls']} 次　"
        f"耗时 {time.time() - started:.1f}s　预算 {result['budget'].get('total')} 元"
    )
    for issue in result["issues"][:3]:
        print(f"  未解决：{issue}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="上线前预检：Key / 接口 / strict 工具模式")
    parser.add_argument("--full", action="store_true", help="额外跑一次完整规划（消耗 token）")
    args = parser.parse_args()

    print("=" * 68)
    print("旅行 Agent 预检")
    print("=" * 68)
    if not check_keys():
        print("\n请先在 .env 中填写两个 Key（值不要加引号、不要有空格），再重新运行本脚本。")
        return 2

    coords = check_amap()
    if coords:
        check_weather(*coords)
        check_route(*coords)
    else:
        print("已跳过天气 / 路径规划检查（需要高德返回的坐标）。")

    check_llm()
    if args.full:
        check_full_run()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("-" * 68)
    print(f"预检结果：{passed}/{len(RESULTS)} 通过")
    if passed != len(RESULTS):
        print("存在未通过的检查项：请把上面的 [FAIL] 行连同报错一起发我。")
        return 1
    print("全部通过。接下来：python selftest.py → python evaluate.py → streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
