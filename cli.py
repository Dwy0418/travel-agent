"""命令行入口：一条命令生成行程单，执行过程实时打印，结果写入 results/。

用法：
    python cli.py --city 梅州市 --days 2 --people 2 --budget 3000 --start 2026-05-01 \
        --companions 带老人 --preferences 少走路 --request "想去文化景点"
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from travel_agent import agent, config, zh_labels

OUT_DIR = Path(__file__).resolve().parent / "results"


def main() -> int:
    parser = argparse.ArgumentParser(description="国内旅行规划智能体（自校验 + 自动重排）")
    parser.add_argument("--city", default="梅州市", help="目的地城市")
    parser.add_argument("--days", type=int, default=2, help="行程天数 1-10")
    parser.add_argument("--people", type=int, default=2, help="出行人数")
    parser.add_argument("--budget", type=float, default=3000, help="总预算（元）")
    parser.add_argument("--start", default=(date.today() + timedelta(days=1)).isoformat(), help="出发日期 YYYY-MM-DD")
    parser.add_argument("--companions", default="", help="同行人，逗号分隔，例如 带老人,带小孩")
    parser.add_argument("--preferences", default="", help="特殊偏好")
    parser.add_argument("--request", default="", help="补充需求")
    parser.add_argument("--quiet", action="store_true", help="不实时打印 Trace")
    args = parser.parse_args()

    missing = config.require_keys()
    if missing:
        print(f"[错误] 缺少密钥：{'、'.join(missing)}。请复制 .env.example 为 .env 并填写。")
        return 2

    def on_trace(entry: dict) -> None:
        if not args.quiet:
            print(entry["line"])

    engine = agent.TravelAgent(trace_callback=on_trace)
    try:
        result = engine.run(
            args.request,
            {
                "city": args.city,
                "days": args.days,
                "people": args.people,
                "budget": args.budget,
                "start_date": args.start,
                "companions": args.companions,
                "preferences": args.preferences,
                "request": args.request,
            },
        )
    except agent.AgentError as exc:
        print(f"[错误] {exc}")
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    out_file = OUT_DIR / f"{args.city}-{args.days}天-{date.today().isoformat()}.md"
    out_file.write_text(result["markdown"], encoding="utf-8")

    print()
    print(result["markdown"])
    print()
    print(f"[完成] 状态={zh_labels.status(result['status'])} 迭代={result['iterations']} "
          f"工具={result['tool_calls']} 耗时={result['elapsed']}秒 预算={result['budget'].get('total')}元")
    print(f"[输出] {out_file}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
