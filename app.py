"""Streamlit 界面：填约束 -> 实时 Trace -> 结构化行程 -> 指标与 Markdown 导出。

运行：streamlit run app.py
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import streamlit as st

from travel_agent import agent, config

st.set_page_config(page_title="国内旅行规划 Agent", page_icon="🧭", layout="wide")

STATUS_TEXT = {"ok": "✅ 校验通过", "max_iterations": "⚠️ 达到迭代上限（仍有问题）", "failed": "❌ 规划失败"}


def render_trace(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        kind = entry.get("kind")
        if kind == "issue":
            st.warning(entry["line"])
        elif kind == "validation" and "全部通过" in entry["line"]:
            st.success(entry["line"])
        elif kind in ("tool_call", "tool_result"):
            st.caption(f"🛠️ {entry['line']}")
        else:
            st.write(entry["line"])


def plan_rows(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, day in enumerate(itinerary.get("days") or []):
        for item in day.get("items") or []:
            rows.append(
                {
                    "天": day.get("day") or index + 1,
                    "日期": day.get("date", ""),
                    "时间": f"{item.get('start', '')}-{item.get('end', '')}",
                    "类型": item.get("type", ""),
                    "安排": item.get("name", ""),
                    "交通": item.get("transport", ""),
                    "花费(元)": item.get("cost", 0),
                    "营业时间": item.get("open_time", ""),
                    "备注": item.get("note", ""),
                }
            )
    return rows


def show_result(result: dict[str, Any]) -> None:
    c = result["constraints"]
    budget = result["budget"]
    cols = st.columns(5)
    cols[0].metric("状态", STATUS_TEXT.get(result["status"], result["status"]))
    cols[1].metric("迭代次数", result["iterations"])
    cols[2].metric("工具调用", result["tool_calls"])
    cols[3].metric("总预算(元)", budget.get("total", 0), delta=f"上限 {c['budget']:.0f}")
    cols[4].metric("耗时(s)", result["elapsed"])

    if result["issues"]:
        with st.expander(f"未解决的校验问题（{len(result['issues'])} 条）", expanded=True):
            for issue in result["issues"]:
                st.warning(issue)

    itinerary = result["itinerary"]
    st.subheader(f"{itinerary.get('city') or c['city']} · {c['days']} 天行程")
    if itinerary.get("summary"):
        st.info(itinerary["summary"])
    st.dataframe(plan_rows(itinerary), width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("预算明细")
        breakdown = budget.get("breakdown") or {}
        st.dataframe(
            [{"项目": k, "金额(元)": v} for k, v in breakdown.items()],
            width="stretch",
            hide_index=True,
        )
    with right:
        st.subheader("注意事项")
        for tip in itinerary.get("tips") or []:
            st.write(f"- {tip}")

    with st.expander("结构化 JSON（可直接入库/二次开发）", expanded=False):
        st.code(json.dumps(itinerary, ensure_ascii=False, indent=2), language="json")

    st.download_button(
        "⬇️ 导出 Markdown 行程单",
        data=result["markdown"],
        file_name=f"{c['city']}-{c['days']}天行程单.md",
        mime="text/markdown",
        width="stretch",
    )
    with st.expander("Markdown 预览", expanded=False):
        st.markdown(result["markdown"])
    with st.expander("执行 Trace（按类型高亮）", expanded=False):
        render_trace(result["trace"])


def main() -> None:
    st.title("🧭 国内旅行规划 Agent（自校验 + 自动重排）")
    st.caption("DeepSeek 决策 · 高德地图取数与路径 · Open-Meteo 天气 · 预算与耗时全部由代码计算 · 校验不过自动重排")

    missing = config.require_keys()
    if missing:
        st.warning(
            "缺少环境变量：" + "、".join(missing) + "。"
            "本地运行请复制 .env.example 为 .env 并填写后重启；"
            "线上（Streamlit Community Cloud）请到 App 右上角 Settings → Secrets，"
            "按 .streamlit/secrets.toml.example 的格式粘贴后点 Save。"
        )

    with st.sidebar:
        st.header("行程约束")
        city = st.text_input("目的地城市", "梅州市")
        start_date = st.date_input("出发日期", date.today() + timedelta(days=1))
        days = st.slider("行程天数", 1, 7, 2)
        people = st.number_input("出行人数", 1, 20, 2)
        budget = st.number_input("总预算（元）", 200, 50000, 3000, step=100)
        companions = st.multiselect("同行人", ["带老人", "带小孩", "情侣", "朋友同行"], default=["带老人"])
        preferences = st.text_input("特殊偏好", "少走路、室内为主")
        request = st.text_area("补充需求", "想去有当地文化特色的地方，节奏轻松些")
        run_clicked = st.button("开始规划", type="primary", width="stretch")

    payload = {
        "city": city,
        "days": int(days),
        "people": int(people),
        "budget": float(budget),
        "start_date": start_date.isoformat(),
        "companions": companions,
        "preferences": preferences,
        "request": request,
    }

    if run_clicked:
        st.session_state.pop("result", None)
        st.subheader("执行 Trace")
        lines: list[str] = []
        log_slot = st.empty()

        def on_trace(entry: dict[str, Any]) -> None:
            lines.append(entry["line"])
            log_slot.code("\n".join(lines[-120:]), language="text")

        try:
            with st.spinner("Agent 正在规划并自我校验……"):
                engine = agent.TravelAgent(trace_callback=on_trace)
                st.session_state["result"] = engine.run(request, payload)
        except agent.AgentError as exc:
            st.error(f"规划失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 界面层兜底，避免白屏
            st.error(f"未预期的错误：{type(exc).__name__}: {exc}")

    result = st.session_state.get("result")
    if result:
        st.divider()
        show_result(result)
    elif not run_clicked:
        st.info("在左侧填写约束后点击「开始规划」。Agent 会先查工具数据，再用代码校验 6 条硬性规则，不通过就自动重排。")


if __name__ == "__main__":
    main()
