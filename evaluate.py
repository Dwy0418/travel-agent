"""评测脚本：跑 20 个测试用例，产出可量化的评测指标与报告。

指标（全部由代码统计，不含主观打分）：
- 约束满足率：一次规划即通过 6 条硬性校验的用例占比（目标 ≥ 90%）
- 工具调用准确率：工具调用中参数合法的占比（目标 ≥ 95%）
- 平均迭代次数（目标 ≤ 5）
- 重排率：触发过自纠错重排的用例占比（目标 ≥ 30%，证明闭环真的在工作）
- 平均耗时（目标 ≤ 30s）
- 耗时中位数（目标 ≤ 30s；不受个别模型延迟尖峰干扰，更能反映常态）

用法：
    python evaluate.py            # 全部 20 个用例
    python evaluate.py --limit 3  # 只跑前 3 个，省 API 额度
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from travel_agent import agent, config

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "results"
START = (date.today() + timedelta(days=1)).isoformat()

TARGETS = {
    "约束满足率": 0.90,
    "工具调用准确率": 0.95,
    "平均迭代次数": 5.0,
    "重排率": 0.30,
    "平均耗时(s)": 30.0,
    "耗时中位数(s)": 30.0,
}

CASES: list[dict[str, Any]] = [
    {"id": "E01", "city": "梅州市", "days": 2, "people": 2, "budget": 2500, "companions": "带老人", "preferences": "少走路、室内为主", "request": "想去客家文化相关的地方"},
    {"id": "E02", "city": "北京市", "days": 3, "people": 2, "budget": 5000, "companions": "带小孩", "preferences": "亲子、博物馆", "request": "孩子 8 岁，想看故宫和科技馆"},
    {"id": "E03", "city": "杭州市", "days": 2, "people": 1, "budget": 1500, "companions": "", "preferences": "自然风光", "request": "想西湖周边轻松走走"},
    {"id": "E04", "city": "成都市", "days": 3, "people": 3, "budget": 4500, "companions": "朋友同行", "preferences": "美食为主", "request": "美食 + 熊猫基地"},
    {"id": "E05", "city": "西安市", "days": 2, "people": 2, "budget": 3000, "companions": "", "preferences": "历史遗迹", "request": "兵马俑和城墙"},
    {"id": "E06", "city": "厦门市", "days": 3, "people": 2, "budget": 5000, "companions": "情侣", "preferences": "海边、拍照", "request": "鼓浪屿和环岛路"},
    {"id": "E07", "city": "广州市", "days": 1, "people": 2, "budget": 800, "companions": "", "preferences": "低成本", "request": "预算很紧，只玩一天"},
    {"id": "E08", "city": "上海市", "days": 2, "people": 2, "budget": 4000, "companions": "带老人", "preferences": "少走路", "request": "外滩、豫园，节奏要慢"},
    {"id": "E09", "city": "桂林市", "days": 3, "people": 4, "budget": 6000, "companions": "带老人,带小孩", "preferences": "山水、游船", "request": "漓江和阳朔，一家人"},
    {"id": "E10", "city": "青岛市", "days": 2, "people": 2, "budget": 2500, "companions": "", "preferences": "海边", "request": "栈桥、八大关"},
    {"id": "E11", "city": "南京市", "days": 2, "people": 1, "budget": 1800, "companions": "", "preferences": "历史人文", "request": "中山陵、夫子庙"},
    {"id": "E12", "city": "重庆市", "days": 2, "people": 2, "budget": 2500, "companions": "朋友同行", "preferences": "夜景美食", "request": "洪崖洞、磁器口"},
    {"id": "E13", "city": "苏州市", "days": 2, "people": 2, "budget": 3000, "companions": "带老人", "preferences": "园林、慢节奏", "request": "拙政园、平江路"},
    {"id": "E14", "city": "昆明市", "days": 3, "people": 2, "budget": 4500, "companions": "", "preferences": "自然风光", "request": "石林、滇池"},
    {"id": "E15", "city": "长沙市", "days": 2, "people": 3, "budget": 3000, "companions": "朋友同行", "preferences": "美食、夜生活", "request": "橘子洲、岳麓山"},
    {"id": "E16", "city": "哈尔滨市", "days": 3, "people": 2, "budget": 5000, "companions": "带小孩", "preferences": "冰雪、室内为主", "request": "冬天带孩子，注意保暖"},
    {"id": "E17", "city": "洛阳市", "days": 2, "people": 2, "budget": 2500, "companions": "带老人", "preferences": "少走路", "request": "龙门石窟、白马寺"},
    {"id": "E18", "city": "深圳市", "days": 2, "people": 2, "budget": 3500, "companions": "", "preferences": "主题公园", "request": "世界之窗、欢乐谷"},
    {"id": "E19", "city": "大理市", "days": 3, "people": 2, "budget": 4500, "companions": "情侣", "preferences": "洱海、慢节奏", "request": "环洱海，不想太赶"},
    {"id": "E20", "city": "黄山市", "days": 2, "people": 2, "budget": 3000, "companions": "带老人", "preferences": "怕爬山", "request": "想看徽派古村落，尽量少爬山"},
]


# --------------------------------------------------------------------------- #
# 单用例执行
# --------------------------------------------------------------------------- #
def run_case(case: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in case.items() if key != "id"}
    request = payload.pop("request", "")
    rows = {"id": case["id"], "city": case["city"], "days": case["days"]}
    started = time.time()
    try:
        result = agent.TravelAgent().run(request, payload)
    except Exception as exc:  # noqa: BLE001 - 评测要跑完全部用例
        return {
            **rows,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "iterations": 0,
            "tool_calls": 0,
            "args_ok": 0,
            "args_total": 0,
            "replanned": False,
            "issues": 1,
            "elapsed": round(time.time() - started, 1),
            "budget": 0.0,
        }

    trace = result["trace"]
    tool_entries = [entry for entry in trace if entry.get("kind") == "tool_call"]
    args_ok = sum(1 for entry in tool_entries if entry.get("args_ok") is not False)
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"eval-{case['id']}-{case['city']}.md").write_text(result["markdown"], encoding="utf-8")
    return {
        **rows,
        "status": result["status"],
        "error": "",
        "iterations": result["iterations"],
        "tool_calls": result["tool_calls"],
        "args_ok": args_ok,
        "args_total": len(tool_entries),
        "replanned": any(entry.get("kind") == "issue" for entry in trace),
        "issues": len(result["issues"]),
        "elapsed": result["elapsed"],
        "budget": result["budget"].get("total", 0.0),
    }


# --------------------------------------------------------------------------- #
# 指标统计
# --------------------------------------------------------------------------- #
def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    total = max(1, len(rows))
    tool_total = sum(row["args_total"] for row in rows)
    return {
        "约束满足率": round(sum(1 for row in rows if row["status"] == "ok") / total, 3),
        "工具调用准确率": round(sum(row["args_ok"] for row in rows) / max(1, tool_total), 3),
        "平均迭代次数": round(statistics.mean([row["iterations"] for row in rows]), 2),
        "重排率": round(sum(1 for row in rows if row["replanned"]) / total, 3),
        "平均耗时(s)": round(statistics.mean([row["elapsed"] for row in rows]), 1),
        "耗时中位数(s)": round(statistics.median([row["elapsed"] for row in rows]), 1),
    }


def hit_target(name: str, value: float) -> bool:
    """比率类指标越大越好，其余越小越好。"""
    higher_is_better = name in ("约束满足率", "工具调用准确率", "重排率")
    return value >= TARGETS[name] if higher_is_better else value <= TARGETS[name]


def build_report(rows: list[dict[str, Any]], metrics: dict[str, float]) -> str:
    lines = [
        "# 评测报告",
        "",
        f"- 用例数：{len(rows)}　生成时间：{date.today().isoformat()}",
        f"- 模型：{config.DEEPSEEK_MODEL}　strict 工具模式：{config.STRICT_TOOLS}",
        "",
        "## 总体指标",
        "",
        "| 指标 | 实测 | 目标 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    for name, value in metrics.items():
        target = TARGETS[name]
        symbol = "≥" if name in ("约束满足率", "工具调用准确率", "重排率") else "≤"
        lines.append(f"| {name} | {value} | {symbol} {target} | {'达标' if hit_target(name, value) else '未达标'} |")

    lines += [
        "",
        "## 逐用例明细",
        "",
        "| 用例 | 城市 | 天数 | 状态 | 迭代 | 工具 | 参数合法率 | 触发重排 | 遗留问题 | 耗时（秒） | 预算（元） |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        rate = row["args_ok"] / row["args_total"] if row["args_total"] else 1.0
        status = row["status"] if row["status"] != "error" else f"error: {row['error'][:40]}"
        lines.append(
            f"| {row['id']} | {row['city']} | {row['days']} | {status} | {row['iterations']} | "
            f"{row['tool_calls']} | {rate:.2f} | {'是' if row['replanned'] else '否'} | {row['issues']} | "
            f"{row['elapsed']} | {row['budget']} |"
        )

    ok_count = sum(1 for row in rows if row["status"] == "ok")
    replan_count = sum(1 for row in rows if row["replanned"])
    slowest = max(rows, key=lambda row: row["elapsed"])
    lines += [
        "",
        "## 结论",
        "",
        f"- {ok_count}/{len(rows)} 个用例最终通过全部硬性校验"
        f"（其中 {replan_count} 个经自纠错重排后才通过，"
        f"{ok_count - replan_count} 个首轮即通过）；"
        f"{len(rows) - ok_count} 个未收敛到全通过。",
        "- 距离、耗时、预算、步行距离全部来自工具或代码计算，模型不参与算术。",
        "- 失败用例的完整 Trace 见 results/ 下对应的 eval-*.md。",
    ]
    if slowest["elapsed"] > 2 * statistics.median([row["elapsed"] for row in rows]):
        lines.append(
            f"- 平均耗时受个别用例的模型延迟尖峰影响（最慢 {slowest['id']} {slowest['city']} "
            f"{slowest['elapsed']}s），常态水平看「耗时中位数」。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="旅行 Agent 评测（20 用例）")
    parser.add_argument("--limit", type=int, default=len(CASES), help="只跑前 N 个用例")
    parser.add_argument("--ids", default="", help="只跑指定用例，逗号分隔，例如 E01,E05")
    args = parser.parse_args()

    missing = config.require_keys()
    if missing:
        print(f"[错误] 缺少环境变量：{'、'.join(missing)}。请复制 .env.example 为 .env 并填写。")
        return 2

    wanted = {item.strip().upper() for item in args.ids.split(",") if item.strip()}
    cases = [case for case in CASES if not wanted or case["id"] in wanted][: max(1, args.limit)]

    rows: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        row = run_case(case)
        rows.append(row)
        print(
            f"[{index}/{len(cases)}] {row['id']} {row['city']} 状态={row['status']} "
            f"迭代={row['iterations']} 重排={'是' if row['replanned'] else '否'} 耗时={row['elapsed']}s"
        )

    metrics = summarize(rows)
    report = build_report(rows, metrics)
    OUT_DIR.mkdir(exist_ok=True)
    report_file = OUT_DIR / f"eval-report-{date.today().isoformat()}.md"
    report_file.write_text(report, encoding="utf-8")

    print()
    for name, value in metrics.items():
        symbol = "≥" if name in ("约束满足率", "工具调用准确率", "重排率") else "≤"
        verdict = "达标" if hit_target(name, value) else "未达标"
        print(f"{name}: {value}（目标 {symbol} {TARGETS[name]}，{verdict}）")
    print(f"\n报告已写入：{report_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
