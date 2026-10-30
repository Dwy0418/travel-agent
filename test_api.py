r"""单步连通性测试：分别验证 高德地理编码 / Open-Meteo 天气 / DeepSeek 三个接口。

用法（项目根目录）：
    .\.venv\Scripts\python.exe test_api.py

每个接口独立测试、互不影响；任一失败都会打印中文定位提示。
不做任何规划逻辑，只回答「Key 通不通」。
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from dotenv import load_dotenv

load_dotenv(BASE / ".env")

from travel_agent import config, llm, tools  # noqa: E402  - 必须在 load_dotenv 之后

LINE = "-" * 68
AMAP_HINTS = {
    "INVALID_USER_KEY": "AMAP_KEY 无效或已被删除：请到高德控制台确认 Key 字符串完整复制（32 位）。",
    "USERKEY_PLAT_NOMATCH": "Key 的「服务平台」选错了：必须选「Web服务」，不能选 Web端(JS API)/Android/iOS。",
    "INVALID_USER_SCODE": "这是「Web端(JS API)」的 Key：本项目是 Python 后端直连 REST，需要重新申请「Web服务」Key。",
    "DAILY_QUERY_OVER_LIMIT": "今日调用量已超免费额度：明天再试，或在控制台提高配额。",
    "SERVICE_NOT_AVAILABLE": "该 Key 未开通此服务：请确认账号已完成实名认证。",
    "INSUFFICIENT_PRIVILEGES": "账号未实名认证或该服务未开通：请在高德控制台完成实名认证。",
}


def show(title: str) -> None:
    print(f"\n{LINE}\n[{title}]")


def fail(which: str, exc: Exception) -> str:
    """打印失败原因并返回定位提示。"""
    text = f"{exc}"
    print(f"  ✗ 失败：{text}")
    for code, hint in AMAP_HINTS.items():
        if code in text.upper():
            return hint
    if "AMAP_KEY" in text:
        return "请在项目根目录 .env 中填写 AMAP_KEY（32 位）。"
    if "DEEPSEEK_API_KEY" in text:
        return "请在项目根目录 .env 中填写 DEEPSEEK_API_KEY。"
    if "timeout" in text.lower() or "连接" in text:
        return "网络不通或超时：检查代理/防火墙，或稍后重试。"
    return "未识别的错误，请把上面这段报错完整发给开发者。"


def main() -> int:
    print("=" * 68)
    print("旅行 Agent · 接口连通性测试")
    print("=" * 68)
    print(f"模型：{config.DEEPSEEK_MODEL}　strict 工具模式：{config.STRICT_TOOLS}")
    print(f"高德 Key：{'已配置' if config.AMAP_KEY else '缺失'}"
          f"（长度 {len(config.AMAP_KEY)}）　"
          f"DeepSeek Key：{'已配置' if config.DEEPSEEK_API_KEY else '缺失'}"
          f"（长度 {len(config.DEEPSEEK_API_KEY)}）")

    hints: list[str] = []
    ok_count = 0

    # 1) 高德地理编码
    show("1/3 高德 · 地理编码 geocode('梅州')")
    coords: dict[str, float] | None = None
    try:
        result = tools.geocode("梅州")
        coords = {"lng": result["lng"], "lat": result["lat"]}
        print(f"  ✓ 成功：{result.get('formatted_address') or result.get('name')} "
              f"→ lng={result['lng']}, lat={result['lat']}")
        ok_count += 1
    except Exception as exc:  # noqa: BLE001 - 连通性测试要吞掉所有异常并给出提示
        hints.append(f"[高德地理编码] {fail('geocode', exc)}")
        traceback.print_exc(limit=1)

    # 2) Open-Meteo 天气（用固定坐标，不依赖第 1 步）
    show("2/3 Open-Meteo · 天气 get_weather(116.12, 24.29, 2)")
    try:
        weather = tools.get_weather(116.12, 24.29, 2)
        # 注意：get_weather 失败时不抛异常，而是返回 success=False（设计为降级按晴天规划）
        if not weather.get("success"):
            raise RuntimeError(weather.get("error") or f"返回异常：{str(weather)[:120]}")
        days = weather.get("days") or []
        if not days:
            raise RuntimeError(f"返回结构异常：{str(weather)[:120]}")
        for day in days:
            rainy = "有雨" if day.get("is_rainy") else "无雨"
            outdoor = "适合户外" if day.get("is_outdoor_ok") else "不宜户外"
            print(
                f"  ✓ {day.get('date')} 降水 {day.get('rain_prob')}%（{rainy}，{outdoor}）"
                f" {day.get('min_temp')}~{day.get('max_temp')}℃"
            )
        ok_count += 1
    except Exception as exc:  # noqa: BLE001
        hints.append("[天气 Open-Meteo] 无需 Key，失败通常是网络问题：检查代理/DNS 后重试。")
        fail("weather", exc)

    # 3) DeepSeek 一句话连通
    show("3/3 DeepSeek · 对话 chat('你好')")
    try:
        message, usage = llm.chat([{"role": "user", "content": "你好"}], max_tokens=20)
        reply = (message.get("content") or "").strip().replace("\n", " ")
        print(f"  ✓ 成功：模型回复「{reply[:60]}」")
        print(f"  ✓ finish_reason={usage.get('finish_reason')} tokens={usage.get('total_tokens')}")
        ok_count += 1
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "402" in text or "Insufficient Balance" in text:
            hint = "DeepSeek 账户余额不足：请到 platform.deepseek.com 充值后再试。"
        elif "401" in text or "Authentication" in text:
            hint = "DEEPSEEK_API_KEY 无效：请在 .env 中重新粘贴（sk- 开头，35 位）。"
        elif "429" in text:
            hint = "请求过于频繁被限流：等 1 分钟再试。"
        else:
            hint = "DeepSeek 调用失败：把上面的报错发给开发者。"
        hints.append(f"[DeepSeek] {hint}")
        fail("llm", exc)

    # 结果汇总
    print(f"\n{LINE}")
    if ok_count == 3:
        print("✅ 所有 API 连通正常（高德 / Open-Meteo / DeepSeek）")
        print("   下一步可执行：python selftest.py")
        return 0

    print(f"⚠️  {ok_count}/3 个接口通过，按下面的提示逐条修复：")
    for item in hints:
        print(f"   - {item}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
