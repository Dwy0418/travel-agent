"""Streamlit 界面：填约束 -> 执行过程 -> 结构化行程 -> 指标与行程单下载。

运行：streamlit run app.py

只负责显示：所有中英文对照来自 travel_agent.zh_labels，
原始参数 / 坐标 / 毫秒耗时写后台日志（logs/trace-detail.log），不上屏。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import streamlit as st

from travel_agent import agent, config, zh_labels

st.set_page_config(page_title=zh_labels.PAGE_TITLE, page_icon="🧭", layout="wide")

KEY_LIMIT = zh_labels.KEY_TRACE_LIMIT


def setup_detail_log() -> None:
    """技术明细写入后台日志文件；写不了就静默降级，不影响页面。"""
    logger = logging.getLogger("travel_agent.detail")
    if logger.handlers:
        return
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(exist_ok=True)
        handler = logging.FileHandler(log_dir / "trace-detail.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    except OSError:
        logger.addHandler(logging.NullHandler())


def key_trace_lines(entries: list[dict[str, Any]]) -> list[str]:
    return [str(entry["line"]) for entry in entries if zh_labels.is_key_trace(entry) and entry.get("line")]


def tech_trace_lines(entries: list[dict[str, Any]]) -> list[str]:
    return [zh_labels.describe_technical(entry) for entry in entries if zh_labels.is_tech_trace(entry)]


def render_key_trace(entries: list[dict[str, Any]], *, expanded: bool = False) -> None:
    """默认只展开关键节点；技术明细收进二级折叠区。"""
    lines = key_trace_lines(entries)
    shown = lines[-KEY_LIMIT:]
    with st.expander(f"执行过程（关键节点 {len(lines)} 条）", expanded=expanded):
        st.code("\n".join(shown) or "暂无记录", language="text")
        if len(lines) > len(shown):
            st.caption(f"仅显示最近 {len(shown)} 条；完整记录见「下载行程单」。")
        with st.expander("技术明细（每次工具调用的参数与耗时）", expanded=False):
            tech = tech_trace_lines(entries)
            st.code("\n".join(tech[-60:]) or "暂无记录", language="text")



def show_result(result: dict[str, Any]) -> None:
    c = result["constraints"]
    budget = result["budget"]

    cols = st.columns(3)
    cols[0].metric("状态", zh_labels.status(result["status"]))
    cols[1].metric("总预算（元）", budget.get("total", 0), delta=f"上限 {c['budget']:.0f}")
    cols[2].metric("耗时（秒）", result["elapsed"])

    with st.expander("执行详情", expanded=False):
        detail_cols = st.columns(3)
        detail_cols[0].metric("迭代次数", result["iterations"])
        detail_cols[1].metric("工具调用", result["tool_calls"])
        detail_cols[2].metric("缓存命中", result.get("cache_hits", 0))

    if result["issues"]:
        with st.expander(f"未解决的校验问题（{len(result['issues'])} 条）", expanded=True):
            for issue in result["issues"]:
                st.warning(issue)

    itinerary = result["itinerary"]
    st.subheader(f"{itinerary.get('city') or c['city']} · {c['days']} 天行程")
    if itinerary.get("summary"):
        st.info(itinerary["summary"])
    st.dataframe(zh_labels.plan_rows(itinerary), width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("预算明细")
        st.dataframe(
            zh_labels.budget_rows(budget.get("breakdown") or {}),
            width="stretch",
            hide_index=True,
        )
    with right:
        st.subheader("注意事项")
        for tip in itinerary.get("tips") or []:
            st.write(f"- {tip}")

    st.download_button(
        "⬇️ 下载行程单",
        data=result["markdown"],
        file_name=f"{c['city']}-{c['days']}天行程单.md",
        mime="text/markdown",
        width="stretch",
    )
    with st.expander("行程单预览", expanded=False):
        st.markdown(result["markdown"])
    with st.expander("结构化数据（供二次开发）", expanded=False):
        st.code(json.dumps(itinerary, ensure_ascii=False, indent=2), language="json")
    render_key_trace(result["trace"])


def main() -> None:
    setup_detail_log()
    st.title(zh_labels.APP_TITLE)
    st.caption(zh_labels.APP_CAPTION)

    missing = config.require_keys()
    if missing:
        st.warning(
            "缺少密钥：" + "、".join(zh_labels.key_label(name) for name in missing) + "。"
            "本地运行请把 .env.example 复制为 .env 并填写对应变量后重启；"
            "线上部署请打开右上角「设置 → 密钥」，按示例格式粘贴后点保存。"
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
        st.subheader("执行过程")
        entries: list[dict[str, Any]] = []
        log_slot = st.empty()

        def on_trace(entry: dict[str, Any]) -> None:
            if not zh_labels.is_key_trace(entry):
                return
            entries.append(entry)
            visible = [str(item["line"]) for item in entries[-KEY_LIMIT:]]
            log_slot.code("\n".join(visible) or "正在准备……", language="text")

        try:
            with st.spinner("正在规划并自我校验……"):
                engine = agent.TravelAgent(trace_callback=on_trace)
                st.session_state["result"] = engine.run(request, payload)
        except agent.AgentError as exc:
            st.error(f"规划失败：{zh_labels.friendly_error(exc)}")
        except Exception:  # noqa: BLE001 - 界面层兜底，避免白屏
            st.error("出现未预期的问题，请稍后重试；若反复出现，请查看后台日志。")

    result = st.session_state.get("result")
    if result:
        st.divider()
        show_result(result)
    elif not run_clicked:
        st.info("在左侧填写约束后点击「开始规划」。系统会先查真实数据，再用代码校验 6 条硬性规则，不通过就自动重排。")


if __name__ == "__main__":
    main()
