"""离线校验详情与卡片数据：问答式解析、点图片出详情、套用卡片直接出行程。

用法：在仓库根目录跑 `python tools/chat_test.py`（AppTest 需要 cwd 是仓库根，
否则 app.py 里的相对路径会指错地方）。

这里会把密钥临时清空，所以「出现规划报错」正好用来证明流程真的开跑了，
而不是只回填了条件——否则每次跑测试都会真的调用一次大模型。
"""
import copy
import re
import sys
from datetime import date, timedelta
from html import unescape
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  ok   " if ok else "  FAIL ") + label + (f" | {detail}" if detail else ""))
    if not ok:
        fails.append(label)


def new_at() -> AppTest:
    # 离线：清空密钥，规划必然报错，既省 token 也让断言稳定
    A.config.DEEPSEEK_API_KEY = ""
    A.config.AMAP_KEY = ""
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    assert not at.exception, f"app 抛异常：{at.exception}"
    return at


def texts(at: AppTest) -> str:
    return "\n".join(md.value for md in at.markdown)


def settle(at: AppTest, timeout: int = 90) -> str:
    """等后台规划线程收尾再重跑一轮，把要说的话取回来。

    线程里的报错不会冒泡到主线程，app 是下一轮才把它渲染成一条 st.error 的。
    离线只是缺密钥，几毫秒就返回，也可能点完那一轮就已经自己收尾切页了，
    所以这里两种情况都要接住：还在跑就等它，已经收干净就直接读页面。
    """
    job = at.session_state.get("_agent_job")
    if job is not None:
        job.finished.wait(timeout=timeout)
        at.run()
        assert not at.exception, f"规划收尾抛异常：{at.exception}"
    return " ".join([str(i.value) for i in at.info] + [str(e.value) for e in at.error])


# ---------------------------------------------------------------------------
# 1. 自然语言解析：只说到的才算数
# ---------------------------------------------------------------------------
cases = [
    ("10月15号和老婆去扬州玩3天，人均2000，想看园林吃淮扬菜",
     {"city": "扬州市", "province": "江苏省", "days": 3, "per_person": 2000}),
    ("国庆带爸妈去桂林5天，预算一万",
     {"city": "桂林市", "days": 5, "budget": 10000}),
    ("去江苏转转，大概4天，我们四个朋友，总预算8000",
     {"province": "江苏省", "days": 4, "people": 4, "budget": 8000}),
    ("明年3月8日到12日全家去三亚，孩子5岁，轻松点别太赶",
     {"city": "三亚市", "people": None}),
    ("大理双廊住两晚，情侣，人均1500",
     {"city": "大理市", "days": 2, "per_person": 1500}),
    ("帮我随便安排一下", {}),
]
for text, want in cases:
    got = A.parse_plan_text(text)
    bad = [f"{k}: 期望{want[k]} 实际{got.get(k)}" for k in want if got.get(k) != want[k]]
    check(not bad, f"解析「{text[:18]}…」", "; ".join(bad))
    if text.startswith("明年3月8日"):
        check(got.get("start") == date(date.today().year + 1, 3, 8)
              and got.get("end") == date(date.today().year + 1, 3, 12),
              "「3月8日到12日」拆成出发 + 返程", f"{got.get('start')}~{got.get('end')}")
    if text.startswith("帮我随便"):
        check(not got, "没提到任何条件时不编造字段", str(got))

check(A.parse_plan_text("10月15号和老婆去扬州玩3天").get("companions") == ["👩 ❤️ 👨 情侣/夫妻"],
      "「和老婆」认成情侣/夫妻")
check("🍲 美食打卡" in A.parse_plan_text("想吃火锅拍照").get("prefs", []), "「想吃」认成美食打卡")

# 人数口语：明确说法都要能读出来
people_cases = {
    "一家三口去北京": 3,
    "一家5口": 5,
    "三口之家": 3,
    "我们两个人": 2,
    "夫妻俩": 2,
    "老两口": 2,
    "两个大人一个小孩": 3,
    "2大2小": 4,
    "带父母共 5 人": 5,
    "一共4人": 4,
    "独自一人": 1,
    "我们仨": 3,
    "夫妻带一个孩子": 3,
    "一个人带两个孩子": 3,
}
for text, want_n in people_cases.items():
    got_n = A.parse_plan_text(text).get("people")
    check(got_n == want_n, f"人数识别「{text}」", f"期望{want_n} 实际{got_n}")
for vague in ("一家人", "几个朋友", "带上家人", "一家老小"):
    check(A.parse_plan_text(vague).get("people") is None,
          f"含混说法不猜人数「{vague}」", str(A.parse_plan_text(vague)))
    check(A._vague_people(vague), f"含混说法能提示确认「{vague}」")
# 相对日期不能倒挂：说过的日子要顺延到明年
late = A.parse_plan_text("1月5日去北京")["start"]
check(late > date.today(), "过去的日期顺延到下一年", str(late))

# ---------------------------------------------------------------------------
# 1a. 全维度抽取：一句话里的天数 / 人均预算 / 人群 / 偏好 / 避雷 / 美食
# ---------------------------------------------------------------------------
full_text = "带爸妈去北京玩4天，人均3000，不想太累，想吃烤鸭"
full = A.parse_plan_text(full_text)
check(full.get("people") == 3 and full.get("days") == 4,
      "同一句话里的人数与天数都抽出来", f"people={full.get('people')} days={full.get('days')}")
check(full.get("per_person") == 3000 and "budget" not in full,
      "「人均3000」只算人均，不混成总预算", str({k: full.get(k) for k in ('per_person', 'budget')}))
check(full.get("companions") == ["👴 带老人"], "「带爸妈」认成带老人", str(full.get("companions")))
check("🚶 少走路/休闲游" in (full.get("prefs") or []) and "🍲 美食打卡" in (full.get("prefs") or []),
      "「不想太累」「想吃」进偏好", str(full.get("prefs")))
check(full.get("avoid") == ["行程别太满"], "「不想太累」同时进避雷", str(full.get("avoid")))
check(full.get("foods") == ["烤鸭"], "点名想吃的菜进 foods", str(full.get("foods")))
check(not full.get("hard") and not full.get("styles"),
      "没说硬需求 / 风格时不瞎补", str({k: full.get(k) for k in ('hard', 'styles')}))

pet = A.parse_plan_text("带宠物去上海，不想爬山")
check(pet.get("hard") == ["宠物友好"] and "🐶 携带宠物" in (pet.get("companions") or []),
      "「带宠物」同时进硬需求与同行人", str(pet.get("hard")))
check(pet.get("avoid") == ["不爬山"], "「不想爬山」进避雷", str(pet.get("avoid")))
wheel = A.parse_plan_text("坐轮椅出行，想看园林")
check(wheel.get("hard") == ["无障碍"] and wheel.get("styles") == ["园林"],
      "轮椅进无障碍硬需求，园林进风格", str({k: wheel.get(k) for k in ('hard', 'styles')}))

# 后端约束：自然语言优先于下拉框，且人数/预算/房间/节奏同一口径
fc = A.agent.build_constraints(
    {"city": "北京市", "days": 2, "people": 2, "budget": 3000, "start_date": "2026-12-01",
     "request": full_text}
)
check(fc["days"] == 4 and fc["people"] == 3, "原话天数/人数覆盖下拉框",
      f"days={fc['days']} people={fc['people']}")
check(fc["budget"] == 9000.0 and fc["rooms"] == 2,
      "人均 3000 × 3 人 = 总预算 9000，房间 ceil(3/2)=2",
      f"budget={fc['budget']} rooms={fc['rooms']}")
check(fc["max_attractions"] == 2 and fc["dwell_hours"] == 4 and fc["rest_required"],
      "带老人 → 每天 2 个景点 / 游览 ≤4 小时 / 必须午休",
      f"{fc['max_attractions']} 个 / {fc['dwell_hours']}h")
check("烤鸭" in fc["foods"] and "行程别太满" in fc["avoid"] and "美食打卡" in fc["preferences"],
      "美食与避雷进了结构化约束", str({k: fc.get(k) for k in ('foods', 'avoid')}))
lines = A.agent.constraint_lines(fc)
check("想吃的当地美食：烤鸭" in lines and "避雷（必须规避）" in lines,
      "提示词里带着美食与避雷，不只是存在字段里")

# 硬约束真的打回：山类景点 / 宠物进室内场馆
_hard_it = {"days": [{"day": 1, "date": "2026-12-01", "items": [
    {"type": "attraction", "name": "香山公园", "start": "09:00", "end": "11:00",
     "lng": 116.19, "lat": 39.99, "indoor": False, "walk_km": 1},
    {"type": "attraction", "name": "故宫博物院", "start": "11:30", "end": "13:00",
     "lng": 116.397, "lat": 39.918, "indoor": True, "walk_km": 1},
    {"type": "food", "name": "全聚德烤鸭", "start": "13:00", "end": "14:00"},
]}]}
_router = lambda name, args: {"success": False}  # noqa: E731 - 离线：天气/路线直接跳过
hill_c = A.agent.build_constraints(
    {"city": "北京市", "days": 1, "people": 2, "budget": 5000, "start_date": "2026-12-01",
     "request": "去北京玩，不想爬山"}
)
hill_issues = A.agent.validator.validate(_hard_it, hill_c, router=_router)
check(any("不想爬山" in issue and "香山公园" in issue for issue in hill_issues),
      "「不想爬山」出现山类景点会被打回", str(hill_issues)[:120])
pet_c = A.agent.build_constraints(
    {"city": "北京市", "days": 1, "people": 2, "budget": 5000, "start_date": "2026-12-01",
     "request": "带宠物去北京"}
)
pet_issues = A.agent.validator.validate(_hard_it, pet_c, router=_router)
check(any("宠物" in issue and "故宫博物院" in issue for issue in pet_issues),
      "「带宠物」进室内场馆会被打回", str(pet_issues)[:120])
food_c = A.agent.build_constraints(
    {"city": "北京市", "days": 1, "people": 2, "budget": 5000, "start_date": "2026-12-01",
     "request": "去北京，想吃火锅"}
)
food_issues = A.agent.validator.validate(_hard_it, food_c, router=_router)
check(any("火锅" in issue for issue in food_issues),
      "点名想吃却没安排对应餐厅会被打回", str(food_issues)[:120])

# ---------------------------------------------------------------------------
# 2. 问答式输入卡：说一句话 -> 条件写回 + 管家复述（侧栏已下线，改吸底 composer）
# ---------------------------------------------------------------------------
at = new_at()
check(not at.sidebar, "侧栏真的没了，页面只剩一条主区")
check("我是你的旅行管家" not in texts(at), "输入卡上不再有管家标题（按需求移除）")
check("请用大白话描述你的出行计划" not in texts(at),
      "副标题也移除，输入卡只剩精确设置 + 输入框 + 开始规划")
check("已套用「" not in texts(at), "「已套用…」提示气泡不再渲染")
# 精确设置退回 st.popover：收起与否是前端状态，AppTest 里控件照样渲染，
# 「AI 填完自动展开」只能在浏览器里点触发按钮，离线那段验的是脚本形状（见 3a）。
check(any(sb.key == "f_country" for sb in at.selectbox), "『精确设置』里的级联下拉照样渲染（收起也在）")
check(any(sb.key == "f_origin" for sb in at.selectbox), "基础信息第一栏就是出发地")
check(at.session_state["f_origin"] == A.ORIGIN_DEFAULT, "出发地默认值落在候选里",
      str(at.session_state["f_origin"]))
check(not any(str(b.key) == "toggle_precise" for b in at.button),
      "输入卡里没有「收起精确设置」那个开关按钮了")
check(any(str(b.label) == "开始规划" for b in at.button), "『开始规划』还在，只是搬进了输入卡")

cards_all, scope_all = A.pick_recommendations()
btn_ct = len([b for b in at.button if str(b.key).startswith("preset_")])
check(btn_ct == len(cards_all) == len({p["key"] for p in cards_all}),
      f"推荐池 {len(cards_all)} 张卡全部进 DOM（多页共存，不再只渲染第一页）", str(btn_ct))
total_pages = -(-len(cards_all) // A.PAGE_SLOTS)
heads = re.findall(r"<div class='pc-page'><span class='pc-page-no'>第 (\d+) / (\d+) 页</span>", texts(at))
check(heads == [("1", str(total_pages))] and total_pages == 9,
      "页码按 ceil(总数/8) 动态算：66 张 = 9 页", str(heads))
last_page_ct = len(cards_all) - (total_pages - 1) * A.PAGE_SLOTS
check(last_page_ct == 2, "末页只放余数 2 张，其余页各 8 个槽位", str(last_page_ct))
check(not re.search(r"pc-pager|pc-pg-|pc-page-nav", "".join(texts(at))),
      "圆形数字分页器（pc-pager 胶囊）已删除")
check(not any(str(b.key) in ("pc_prev", "pc_next") for b in at.button),
      "右上角上一页 / 下一页按钮已移除")
check(A._card_face(A.CITY_CARDS["北京市"][0], 8, 0).count("animation-delay:0ms") == 1,
      "错落延迟按页内算：第二页第一张不排队")

snap_src = (ROOT / "app.py").read_text(encoding="utf-8")
check("cards[page * PAGE_SLOTS: (page + 1) * PAGE_SLOTS]" in snap_src
      and "for row_start in (0, 4)" in snap_src,
      "每页按 8 个槽位切、两行各 4 张生成")
check("scroll-snap-type: y mandatory" in snap_src
      and "scroll-snap-align: start" in snap_src
      and "height: 100dvh !important" in snap_src,
      "整页吸附存在：100dvh + y mandatory + start")
check("addEventListener('wheel'" in snap_src and "passive: false" in snap_src
      and "locked = now + 800" in snap_src,
      "滚轮监听 passive:false，切页后冻结 800ms")
check("sc.scrollTo({ top: target * sc.clientHeight, behavior: 'smooth' })" in snap_src,
      "滚轮一次只平滑跳到目标页，不会连飞两三页")
check("root.classList.toggle('ci-last-page'" in snap_src
      and "html.ci-last-page .st-key-composer" in snap_src
      and "transform: translateY(calc(100% + 24px))" in snap_src,
      "输入卡回到远端外观：默认藏在视口下，末页由 IntersectionObserver 用 translateY 滑进来")
check(".st-key-precise" not in snap_src, "整页摊开那张表单卡的样式已删除，精确设置回到弹层")
grid_rules = "\n".join(re.findall(r":has\(\.pc-face\)[^{]*\{[^}]*\}", snap_src))
check("repeat(6" not in grid_rules, "卡片网格列数只剩 8 的因数（1/2/4），末行不缺格")
check("content-visibility" not in snap_src, "页外网格的 content-visibility 随三页同屏一起退场")
check(".rec-head" not in snap_src and ".pc-page-scope {" in snap_src,
      "旧的一屏标题样式摘干净了，范围提示改由每页页首接手")

# 输入框全局唯一：一张卡，fixed 钉底，翻页时只升不挪
composer_css = re.search(r"\.st-key-composer \{([^}]*)\}", snap_src)
assert composer_css, "app.py 里找不到 .st-key-composer 规则"
cbody = composer_css.group(1)
check("position: fixed" in cbody and "bottom:" in cbody, "输入卡是 fixed 钉底，不随主区滚动", cbody[:50].strip())
check("z-index: 30" in cbody, "层级退回远端的 30，卡片从它下面滚过去")
check("visibility: hidden" in cbody and "opacity: 0" in cbody,
      "默认整卡藏在视口外，末页才升起，不盖第一页的卡片")
check("max-height" not in cbody and "overflow-y" not in cbody,
      "卡里不放对话记录，也就不需要自带滚动")
check(snap_src.count('with st.container(key="composer"):') == 1, "全页只有一张输入卡",
      str(snap_src.count('with st.container(key="composer"):')))
check("st.container(height=172" not in snap_src, "输入卡里那段定高对话回看已撤掉")
pads = [int(v) for v in re.findall(r"padding-bottom: calc\(var\(--ci-composer-h, (\d+)px\)", snap_src)]
check(bool(pads) and max(pads) >= 200,
      "主区底部留白跟着输入卡实时高度走（兜底 ≥200px），末排卡片不被盖住", str(pads))
check("_embed_layout_sync()" in snap_src and "--ci-composer-h" in snap_src,
      "输入卡高度由 _embed_layout_sync 写进 CSS 变量，主区留白跟着它让位")
check("if not result and not detail_open:" in snap_src,
      "详情弹窗打开时根本不渲染输入卡，不与弹窗抢位置")
comp_fn = re.search(r"def render_composer\(\) -> None:[\s\S]*?\n(?=def )", snap_src)
check(comp_fn and 'popover_or_expander("⚙️ 精确设置' in comp_fn.group(0),
      "精确设置回到输入卡里的 popover 弹层")
check(comp_fn and '_constraint_fields("f_")' in comp_fn.group(0),
      "级联下拉就住在弹层里，不再摊进正文")
check("def render_precise_panel" not in snap_src, "整页摊开那张表单卡的函数已删除")
head_body = snap_src[snap_src.index("def main() -> None:"):]
check("render_precise_panel()" not in head_body, "main 里不再往正文插表单卡")

# 冷启动闪烁的根因是「卡片 CSS 出现得太晚」：脚本从 hero 到卡片中间要跑一千多毫秒，
# 等 background-image 生效才发请求，第一页的封面就空着那一秒多。preload 必须挂在最早的块里。
# 翻页之后只预加载第一页：首屏只等 8 张，后两页晚一点到也不挡着看第 1 页。
idx_hero = next((i for i, m in enumerate(at.markdown) if "hero-title" in str(m.value)), -1)
idx_css = next((i for i, m in enumerate(at.markdown) if ".pc-face-0" in str(m.value)), -1)
hero_md = str(at.markdown[idx_hero].value) if idx_hero >= 0 else ""
css_md = str(at.markdown[idx_css].value) if idx_css >= 0 else ""
pre = [unescape(u) for u in re.findall(r'<link rel="preload" as="image" href="([^"]+)"', hero_md)]
need = re.findall(r"background-image: url\('([^']+)'\)", css_md)
check(0 <= idx_hero < idx_css, "preload 比卡片 CSS 更早发出", f"hero={idx_hero} css={idx_css}")
check(len(pre) == A.PAGE_SLOTS and set(pre) == set(need[:A.PAGE_SLOTS]),
      f"第一页 {A.PAGE_SLOTS} 张封面预加载，其余交给吸附时再取", f"preload={len(pre)} 第一页={A.PAGE_SLOTS} 全部={len(need)}")
# 一页 8 张之后共 24 张封面，Chrome 的 content-visibility 并不会推迟取图，只能把每张要图压小。
# 4 列栅格一列约 350px，dpr=2 也只要 700px；preload 和 CSS 必须同一个尺寸，否则等于下两遍。
check(need and all("w=720" in u and "q=72" in u for u in need) and "w=900" not in css_md,
      "封面统一要 720px 小图，24 张的流量不超过从前 12 张", f"{len(need)} 张，样例={need[0][:60] if need else '无'}")

# 出发地：省 -> 市二级联动，默认「北京市 / 北京市」
origin_prov = next((s for s in at.selectbox if s.key == "f_origin_province"), None)
origin_city = next((s for s in at.selectbox if s.key == "f_origin"), None)
check(origin_prov is not None and origin_prov.value == "北京市",
      "出发省份默认北京市", str(origin_prov.value) if origin_prov else "无")
check(origin_city is not None and origin_city.value == "北京市",
      "出发城市默认北京市", str(origin_city.value) if origin_city else "无")
check(len(A.regions.PROVINCES) == 34 and all(A.regions.cities_of(p) for p in A.regions.PROVINCES),
      "34 个省级行政区都有下辖地级行政区", str(len(A.regions.PROVINCES)))
check(A._origin_province_of("成都市") == "四川省" and A._origin_province_of("北京市") == "北京市",
      "城市能反查回省份")

at.selectbox(key="f_origin_province").set_value("四川省").run()
assert not at.exception, f"换省份抛异常：{at.exception}"
check(at.session_state["f_origin"] == "成都市",
      "换成四川省后城市自动落到第一个地级市", str(at.session_state["f_origin"]))
at.selectbox(key="f_origin").set_value("甘孜藏族自治州").run()
assert not at.exception, f"选自治州抛异常：{at.exception}"
check(at.session_state["f_origin"] == "甘孜藏族自治州", "任意地级行政区都能选到",
      str(at.session_state["f_origin"]))
at.selectbox(key="f_origin_province").set_value("北京市").run()
check(at.session_state["f_origin"] == "北京市", "切回直辖市时城市跟着回落")

# 大致想法：唯一自由输入区；同行人 / 偏好模块已删
idea_widget = next((t for t in at.text_area if t.key == "f_idea"), None)
check(idea_widget is not None, "「大致想法」输入区在 composer 里", str([t.key for t in at.text_area]))
check("📝 大致想法" in texts(at), "大致想法有独立标题")
captions = "\n".join(str(c.value) for c in at.caption)
check("(如带老人或者想要室内景点多一点请说明)" in captions, "提示文案照需求写", captions[:40])
field_labels = " ".join(str(c.label) for c in at.checkbox)
check("独自出行" not in field_labels and "室内为主" not in field_labels,
      "同行人 / 偏好的多选项已从表单删除", field_labels[:60])

at.text_area(key="f_idea").set_value("带老人，想去室内博物馆，别太累").run()
assert not at.exception, f"写大致想法抛异常：{at.exception}"
derived = A._idea_constraints(at.session_state["f_idea"])
check(derived["companions"] == ["带老人"], "从大致想法提取出带老人", str(derived["companions"]))
check("室内为主" in derived["preferences"] and "少走路/休闲游" in derived["preferences"],
      "室内与不想太累都识别成偏好", derived["preferences"])
at.selectbox(key="f_province").set_value("北京市").run()
at.selectbox(key="f_city").set_value("北京市").run()
next(b for b in at.button if str(b.label) == "开始规划").click()
at.run()
assert not at.exception, f"点开始规划抛异常：{at.exception}"
last = at.session_state.get("_last_payload") or {}
check(last.get("request") == "带老人，想去室内博物馆，别太累"
      and last.get("origin") == "北京市"
      and last.get("companions") == ["带老人"],
      "开始规划把出发地 / 原话 / 派生约束一起打包",
      f"origin={last.get('origin')} request={last.get('request')!r}")
check(last.get("people") == 2, "文案没写人数时，保持人数框当前值", str(last.get("people")))

# ---------------------------------------------------------------------------
# 2a0. 必填项前置校验：缺失时只在按钮下提示，不建任务、不写状态、不二次 rerun
# ---------------------------------------------------------------------------
check(A._missing_requirements({"origin": "北京市", "people": 2, "budget": 3000}, "") == ["目的地"],
      "空白条件只缺目的地（出发地/日期/人数/预算都有默认值）",
      str(A._missing_requirements({"origin": "北京市", "people": 2, "budget": 3000}, "")))
check(A._missing_requirements({"origin": "", "people": 2, "budget": 3000}, "") == ["出发地", "目的地"],
      "出发地和目的地都空时两个都点名",
      str(A._missing_requirements({"origin": "", "people": 2, "budget": 3000}, "")))
check(A._missing_requirements({"origin": "北京市", "people": 2, "budget": 3000}, "去成都玩3天") == [],
      "目的地写在「大致想法」里也算填了",
      str(A._missing_requirements({"origin": "北京市", "people": 2, "budget": 3000}, "去成都玩3天")))

ax = new_at()
next(b for b in ax.button if str(b.label) == "开始规划").click()
ax.run()
assert not ax.exception, f"缺必填项点开始规划抛异常：{ax.exception}"
check(ax.session_state.get("_agent_job") is None and ax.session_state.get("_last_payload") is None,
      "缺目的地时不建规划任务、不发请求",
      f"job={ax.session_state.get('_agent_job')} payload={ax.session_state.get('_last_payload')}")
check(not ax.session_state.get("_run_now"), "缺目的地时不写 _run_now，不触发第二轮 rerun")
check(any("还差目的地" in str(m.value) for m in ax.markdown),
      "提示直接渲染在页面上（按钮下方那一行）",
      "; ".join(str(m.value)[:40] for m in ax.markdown if "还差" in str(m.value))[:120])
check(not any("还缺一个具体城市" in str(w.value) for w in ax.warning),
      "不再往页面顶部丢「还缺城市」警告条",
      "; ".join(str(w.value)[:30] for w in ax.warning)[:100])

ay = new_at()
ay.text_area(key="f_idea").set_value("去成都玩3天，想吃火锅").run()
next(b for b in ay.button if str(b.label) == "开始规划").click()
ay.run()
assert not ay.exception, f"文案里有目的地时开跑抛异常：{ay.exception}"
check(ay.session_state.get("_last_payload") is not None,
      "文案里写了目的地就放行，正常开跑",
      str(ay.session_state.get("_last_payload"))[:60])
settle(ay)

# ---------------------------------------------------------------------------
# 2a1. 花费只有一套口径：明细逐项累加；UI / PDF 同源；超预算必须被标记
# ---------------------------------------------------------------------------
bug_items = [
    {"type": "transport", "name": "打车", "start": "09:00", "end": "09:20", "cost": 11},
    {"type": "transport", "name": "打车", "start": "09:30", "end": "09:45", "cost": 7},
    {"type": "transport", "name": "打车", "start": "10:00", "end": "10:15", "cost": 10},
    {"type": "transport", "name": "打车", "start": "11:00", "end": "11:15", "cost": 10},
    {"type": "transport", "name": "打车", "start": "15:00", "end": "15:15", "cost": 9},
    {"type": "food", "name": "午餐", "start": "12:00", "end": "13:00", "cost": 20},
    {"type": "hotel", "name": "青旅床位", "start": "20:00", "end": "20:30", "cost": 20},
]
bug_itin = {
    "city": "测试市",
    "summary": "预算 200 的两天行程",
    "days": [{"day": 1, "date": "2026-01-01", "items": bug_items}],
    "tips": ["总预算约198元（交通108+餐饮70+住宿20）", "带好身份证"],
}
bug_cost = A.tools.summarize_cost(bug_itin)
check(bug_cost["total"] == 87 and bug_cost["breakdown"] == {"transport": 47, "food": 20, "hotel": 20},
      "明细逐项累加：47+20+20=87，不再出现 food300 / hotel200",
      f"{bug_cost['breakdown']} total={bug_cost['total']}")
check(sum(bug_cost["breakdown"].values()) == bug_cost["total"] and bug_cost["source"] == "items",
      "总价就是明细累加（同一份结果，没有第二套公式）", str(bug_cost))
check(A.tools.apply_cap(dict(bug_cost), 200)["over"] is False,
      "87 <= 200 不标超预算", str(A.tools.apply_cap(dict(bug_cost), 200)))
over = A.tools.apply_cap({"total": 582}, 200)
check(over["over"] is True and over["over_by"] == 382,
      "582 > 200 必须标记超预算 ¥382", str(over))
check(A.agent._sanitize_tips(bug_itin["tips"]) == ["带好身份证。"],
      "带金额的小贴士被删掉，只留文字建议", str(A.agent._sanitize_tips(bug_itin["tips"])))
check("必须单列一条 type=transport" in A.agent.SYSTEM_PROMPT,
      "提示词：付费出行必须单列 transport 条目并写 cost",
      "提示词里没有车费单列要求")

# ¥3000 那单的真实形态：车费只写在景点行的 transport 字段，明细里没有任何交通条目。
b3000_items = [
    {"start": "09:00", "end": "10:30", "type": "attraction", "name": "天安门广场",
     "lng": 116.391, "lat": 39.903, "cost": 0, "transport": "driving", "walk_km": 1.2, "note": "广场平坦"},
    {"start": "10:45", "end": "12:00", "type": "attraction", "name": "中国国家博物馆",
     "lng": 116.401, "lat": 39.905, "cost": 0, "transport": "walking", "walk_km": 1.2, "note": ""},
    {"start": "12:10", "end": "13:00", "type": "food", "name": "周边午餐",
     "lng": 116.405, "lat": 39.907, "cost": 0, "transport": "walking", "walk_km": 0.4, "note": ""},
    {"start": "13:15", "end": "14:45", "type": "food", "name": "大董烤鸭",
     "lng": 116.410, "lat": 39.910, "cost": 321, "transport": "driving", "walk_km": 0, "note": ""},
    {"start": "15:00", "end": "16:00", "type": "hotel", "name": "王府井酒店",
     "lng": 116.410, "lat": 39.910, "cost": 400, "transport": "driving", "walk_km": 0, "note": ""},
]
b3000_itin = {
    "city": "北京市",
    "summary": "",
    "days": [{"day": 1, "date": "2026-11-20", "weather_note": "", "items": b3000_items}],
    "tips": [],
}
b3000_cons = A.agent.build_constraints(
    {"city": "北京市", "days": 1, "people": 3, "budget": 9000, "companions": "带老人"}
)


def _offline_router(name: str, args: dict[str, Any], draft: dict[str, Any] | None = None) -> dict[str, Any]:
    if name == "plan_route":
        return {"success": True, "mode": args.get("mode"), "distance_m": 5000,
                "duration_sec": 900, "cost_yuan": 30, "estimated": False}
    return {"success": False, "error": "offline"}


transport_issues = A.agent.validator.validate(b3000_itin, b3000_cons, router=_offline_router)
check(any("没有单独的交通花费条目" in one for one in transport_issues),
      "校验：景点行写了付费出行却没车费条目，要求补车费", "；".join(transport_issues)[:160])
check(any("是正餐却标了免费" in one for one in transport_issues),
      "校验：正餐 cost=0 被拦，要求补合理金额", "；".join(transport_issues)[:160])

_real_execute = A.tools.execute_tool
A.tools.execute_tool = _offline_router
try:
    _engine = A.agent.TravelAgent(trace_callback=lambda e: None, chat=lambda *a, **k: ({}, {}))
    _engine.state = {"tool_results_cache": {}, "trace_log": []}
    _leg_cost, _ = _engine._leg_transport(b3000_itin)
    _added = A.agent._ensure_transport_items(b3000_itin, _engine.state.get("leg_by_day") or {})
finally:
    A.tools.execute_tool = _real_execute
_b3000_sum = A.tools.summarize_cost(b3000_itin)
_b3000_item_sum = round(
    sum(float(one.get("cost") or 0) for d in b3000_itin["days"] for one in d["items"]), 1
)
check(_leg_cost > 0 and _added >= 1 and _b3000_sum["breakdown"].get("transport", 0) > 0,
      "兜底：没有交通条目时补出 transport 花费", f"leg={_leg_cost} added={_added} {_b3000_sum['breakdown']}")
check(_b3000_sum["total"] == _b3000_item_sum,
      "兜底：补条目后总额仍等于明细逐项累加", f"total={_b3000_sum['total']} items={_b3000_item_sum}")

_b3000_table = A._table_rows(b3000_itin["days"])
_table_costs = re.findall(r"<td class='itn-cost'>(.*?)</td>", _b3000_table, flags=re.S)
_table_sum = 0.0
for _cell in _table_costs:
    if "免费" in _cell:
        continue
    _digits = re.sub(r"[^\d.]", "", _cell)
    if _digits:
        _table_sum += float(_digits)
check(("市内交通（估算）" in _b3000_table and "¥60" in _b3000_table),
      "兜底条目在表格可见且带金额", _b3000_table[-220:])
check((abs(round(_table_sum, 1) - _b3000_sum["total"]) < 0.01),
      "兜底后表格逐行金额之和等于 total",
      f"table_sum={round(_table_sum, 1)} total={_b3000_sum['total']}")

_engine2 = A.agent.TravelAgent(trace_callback=lambda e: None, chat=lambda *a, **k: ({}, {}))
_engine2.state = {"tool_results_cache": {}, "trace_log": []}
_leg2, _ = _engine2._leg_transport(bug_itin)
_added2 = A.agent._ensure_transport_items(bug_itin, _engine2.state.get("leg_by_day") or {})
check(_leg2 == 0 and _added2 == 0,
      "兜底：已有正常交通条目不重复累加", f"leg={_leg2} added={_added2}")

# 大交通：飞机 / 高铁 / 自驾各生成一条去程、一条返程，写进首末日明细
_lh_days = [
    {"day": 1, "date": "2026-12-01", "weather_note": "", "items": [
        {"start": "09:00", "end": "11:00", "type": "attraction", "name": "外滩", "lng": 121.49, "lat": 31.24, "cost": 0},
        {"start": "13:00", "end": "15:00", "type": "attraction", "name": "豫园", "lng": 121.49, "lat": 31.23, "cost": 40},
        {"start": "15:10", "end": "17:30", "type": "attraction", "name": "南京路", "lng": 121.47, "lat": 31.24, "cost": 0},
    ]},
    {"day": 2, "date": "2026-12-02", "weather_note": "", "items": [
        {"start": "09:00", "end": "11:00", "type": "attraction", "name": "博物馆", "lng": 121.47, "lat": 31.23, "cost": 0},
        {"start": "12:00", "end": "13:00", "type": "food", "name": "午餐", "lng": 121.47, "lat": 31.23, "cost": 40},
    ]},
    {"day": 3, "date": "2026-12-03", "weather_note": "", "items": [
        {"start": "09:00", "end": "11:00", "type": "attraction", "name": "公园", "lng": 121.46, "lat": 31.22, "cost": 0},
        {"start": "13:00", "end": "15:00", "type": "attraction", "name": "美术馆", "lng": 121.46, "lat": 31.22, "cost": 60},
        {"start": "15:10", "end": "17:30", "type": "attraction", "name": "商场", "lng": 121.46, "lat": 31.22, "cost": 0},
    ]},
]
for _mode, _distance in (("flight", 1200), ("high_speed", 1000), ("self_drive", 500)):
    _lh_itin = {"city": "上海市", "summary": "", "tips": [], "days": copy.deepcopy(_lh_days)}
    _lh_cons = A.agent.build_constraints({
        "origin": "北京市", "city": "上海市", "days": 3, "people": 2,
        "budget": 6000, "transport_mode": _mode,
    })
    _lh_info = A.long_haul.apply(_lh_itin, _lh_cons, _distance, (116.4, 39.9), (121.47, 31.23), realtime=False)
    _lh_sum = A.tools.summarize_cost(_lh_itin)
    _lh_table = A._table_rows(_lh_itin["days"])
    _lh_cells = re.findall(r"<td class='itn-cost'>(.*?)</td>", _lh_table, flags=re.S)
    _lh_sum_cells = 0.0
    for _cell in _lh_cells:
        if "免费" in _cell:
            continue
        _digits = re.sub(r"[^\d.]", "", _cell)
        if _digits:
            _lh_sum_cells += float(_digits)
    check(("去程：" in _lh_table and "返程：" in _lh_table and "估算" in _lh_table and _lh_info["total"] > 0),
          f"大交通[{_mode}]：去程/返程在表格可见并标注估算",
          _lh_table[:180])
    check((_lh_info.get("source") == "estimate" and "非实时" in _lh_info.get("note", "")),
          f"大交通[{_mode}]：无实时数据时明确标注估算/非实时",
          f"source={_lh_info.get('source')} note={_lh_info.get('note', '')[:60]}")
    check((abs(round(_lh_sum_cells, 1) - _lh_sum["total"]) < 0.01),
          f"大交通[{_mode}]：表格累加 == total",
          f"table={round(_lh_sum_cells, 1)} total={_lh_sum['total']}")
    check((len([x for x in _lh_itin["days"][0]["items"] if x.get("type") == "attraction"]) <= 2
           and len([x for x in _lh_itin["days"][-1]["items"] if x.get("type") == "attraction"]) <= 2),
          f"大交通[{_mode}]：首末日各留半天", str(_lh_itin["days"][0]["items"]))
    check((len(_lh_itin["days"][1]["items"]) == len(_lh_days[1]["items"])),
          f"大交通[{_mode}]：中间日不变", str(len(_lh_itin["days"][1]["items"])))
    _lh_over = A.tools.apply_cap(dict(_lh_sum), 200)
    check((_lh_over.get("over") and _lh_over.get("over_by", 0) > 0),
          f"大交通[{_mode}]：往返票计入预算后可判定超支",
          str(_lh_over))

fake = {
    "status": "ok",
    "itinerary": dict(bug_itin, tips=A.agent._sanitize_tips(bug_itin["tips"])),
    "constraints": {
        "city": "测试市", "days": 1, "people": 2, "budget": 200, "rooms": 1,
        "start_date": "2026-01-01", "companions": [], "preferences": "",
    },
    "budget": A.tools.apply_cap(dict(bug_cost), 200),
    "issues": [], "iterations": 1, "tool_calls": 0, "cache_hits": 0,
    "elapsed": 1.0, "trace": [], "usage": {}, "markdown": "",
}
# PDF 与页面读的是同一个 result["budget"]：拦住格式化函数，确认它拿到的就是 87/200，没有 582
pdf_money: list[Any] = []
_orig_money = A.pdf_export._money


def _spy_money(value: Any, free: str = "免费") -> str:
    pdf_money.append(value)
    return _orig_money(value, free=free)


A.pdf_export._money = _spy_money
try:
    A.pdf_export.build_itinerary_pdf(fake)
finally:
    A.pdf_export._money = _orig_money
pdf_numbers = {float(v) for v in pdf_money if isinstance(v, (int, float))}
check(87.0 in pdf_numbers and 200.0 in pdf_numbers and 582.0 not in pdf_numbers,
      "PDF 总价与明细累加同源（读到 87/200，没有 582）", str(sorted(pdf_numbers)))

at_bug = new_at()
at_bug.session_state["result"] = fake
at_bug.run()
assert not at_bug.exception, f"渲染预算结果抛异常：{at_bug.exception}"
ui_text = texts(at_bug)
check(ui_text.count("¥87") >= 2 and "¥582" not in ui_text and "198" not in ui_text,
      "顶部预算条与预算卡片读同一个 ¥87，页面上没有第二个金额来源",
      f"¥87 ×{ui_text.count('¥87')}")

# 超预算必须在页面上明示，并给出「一键极限压缩预算」入口，不能按正常规划默默展示。
# 注意：show_result 会用明细重算 budget，所以必须把明细本身改成 582，不能只塞一个 582 的 budget。
fake_over = copy.deepcopy(fake)
_transport_set = False
for _day in fake_over["itinerary"]["days"]:
    for _item in _day.get("items") or []:
        if _item.get("type") == "transport":
            _item["cost"] = 0 if _transport_set else 82
            _transport_set = True
        elif _item.get("type") in ("food", "meal"):
            _item["cost"] = 300
        elif _item.get("type") == "hotel":
            _item["cost"] = 200
at_over = new_at()
at_over.session_state["result"] = fake_over
at_over.run()
assert not at_over.exception, f"渲染超预算结果抛异常：{at_over.exception}"
over_text = texts(at_over)
check("超出预算" in over_text and "¥382" in over_text,
      "超预算 ¥382 在页面上明示", over_text[-120:])
check(any("极限压缩" in str(b.label) for b in at_over.button),
      "超预算时提供「一键极限压缩预算」按钮",
      str([str(b.label) for b in at_over.button])[:120])

# ---------------------------------------------------------------------------
# 2a2. 人数从大致想法提取：明确说法覆盖人数框，含混说法保持原值并提示
# ---------------------------------------------------------------------------
ap = new_at()
ap.number_input(key="f_people").set_value(5).run()
ap.text_area(key="f_idea").set_value("一家三口去北京，带老人，想去室内博物馆").run()
check(any("🧭 已识别：3 人" in str(c.value) for c in ap.caption),
      "输入时就提示识别到 3 人", "; ".join(str(c.value)[:40] for c in ap.caption)[:100])
next(b for b in ap.button if str(b.label) == "开始规划").click()
ap.run()
assert not ap.exception, f"一家三口开跑抛异常：{ap.exception}"
check(ap.session_state["f_people"] == 3, "一家三口把人数框从 5 改成 3",
      str(ap.session_state.get("f_people")))
check(any("已按大致想法同步：3 人" in str(t.value) for t in ap.toast),
      "开跑时用 toast 明确告知人数已改", [str(t.value)[:40] for t in ap.toast])
last3 = ap.session_state.get("_last_payload") or {}
check(last3.get("people") == 3 and last3.get("city") == "北京市",
      "payload 与界面人数一致", f"people={last3.get('people')} city={last3.get('city')}")
settle(ap)

aq = new_at()
aq.number_input(key="f_people").set_value(5).run()
aq.text_area(key="f_idea").set_value("我们两个人去北京").run()
next(b for b in aq.button if str(b.label) == "开始规划").click()
aq.run()
assert not aq.exception, f"两个人开跑抛异常：{aq.exception}"
check(aq.session_state["f_people"] == 2, "「我们两个人」把人数框从 5 改成 2",
      str(aq.session_state.get("f_people")))
last2 = aq.session_state.get("_last_payload") or {}
check(last2.get("people") == 2, "payload 跟着变成 2 人", str(last2.get("people")))
settle(aq)

av = new_at()
av.number_input(key="f_people").set_value(4).run()
av.text_area(key="f_idea").set_value("一家人去北京").run()
check(any("没听出具体人数" in str(c.value) for c in av.caption),
      "含混说法在输入区提示确认人数", "; ".join(str(c.value)[:40] for c in av.caption)[:100])
next(b for b in av.button if str(b.label) == "开始规划").click()
av.run()
assert not av.exception, f"含混说法开跑抛异常：{av.exception}"
check(av.session_state["f_people"] == 4, "「一家人」不猜人数，保持 4 人",
      str(av.session_state.get("f_people")))
check(any("没听出具体人数" in str(t.value) for t in av.toast),
      "含混说法开跑时提示按当前人数", [str(t.value)[:40] for t in av.toast])
settle(av)

# 2a3. 天数 / 人均预算也按自然语言覆盖：界面显示与 payload 必须同一个数
aw = new_at()
aw.text_area(key="f_idea").set_value(full_text).run()
next(b for b in aw.button if str(b.label) == "开始规划").click()
aw.run()
assert not aw.exception, f"全维度开跑抛异常：{aw.exception}"
check(aw.session_state["f_people"] == 3, "人数框同步成 3", str(aw.session_state.get("f_people")))
check(A._span_days(aw.session_state["f_start"], aw.session_state["f_end"]) == 4,
      "日期框同步成 4 天",
      f"{aw.session_state.get('f_start')}~{aw.session_state.get('f_end')}")
check(int(aw.session_state["f_budget"]) == 9000, "预算框同步成 9000（人均 3000 × 3）",
      str(aw.session_state.get("f_budget")))
toasts = [str(t.value) for t in aw.toast]
lastw = aw.session_state.get("_last_payload") or {}
check(lastw.get("days") == 4 and lastw.get("people") == 3 and lastw.get("budget") == 9000.0,
      "payload 与界面三个数字一致",
      f"days={lastw.get('days')} people={lastw.get('people')} budget={lastw.get('budget')}")
check(lastw.get("per_person") == 3000 and lastw.get("foods") == ["烤鸭"]
      and lastw.get("avoid") == ["行程别太满"],
      "人均、美食、避雷一起送进 payload", str({k: lastw.get(k) for k in ('per_person', 'foods', 'avoid')}))
check(any("4 天" in t and "9,000" in t for t in toasts),
      "toast 同时告知天数与预算已改", str(toasts)[:120])
settle(aw)
# ---------------------------------------------------------------------------
# 2b. 地标 -> 省市：「嘉应大学附近」必须认成广东梅州，且只填说到的那几栏
# ---------------------------------------------------------------------------
tree_pairs = {(prov, city) for provinces in A.DESTINATION_TREE.values()
              for prov, cities in provinces.items() for city in cities}
bad_alias = [f"{name}→{prov}{city}" for name, (prov, city) in A.LANDMARK_ALIAS.items()
             if (prov, city) not in tree_pairs]
check(not bad_alias, "地标表的省市全在目的地树里，填进去不会被下拉框退回「不限」", "; ".join(bad_alias[:3]))
check(A.LANDMARK_ALIAS["嘉应大学"] == ("广东省", "梅州市"), "收录表里嘉应大学就是梅州")
check(all(A._country_of(prov) in A.DESTINATION_TREE for prov, _ in A.LANDMARK_ALIAS.values()),
      "地标反查出的国家标签都是下拉框里的真选项")

ji = A.parse_plan_text("嘉应大学附近好玩的地方")
check(ji.get("city") == "梅州市" and ji.get("province") == "广东省",
      "「嘉应大学附近好玩的地方」认成广东省梅州市", str(ji))
check(ji.get("landmark") == "嘉应大学", "地标名留在结果里，回复才说得出「按地标定位」")
check("days" not in ji and "budget" not in ji and "people" not in ji,
      "一句话里没说的天数/预算/人数不瞎补", str(sorted(ji)))
check(A.parse_plan_text("下周去乌镇玩两天").get("city") == "嘉兴市", "「乌镇」认到嘉兴，不是编一个市")
check(A.parse_plan_text("帮我随便安排一下").get("landmark") is None, "没有地标时不硬凑一个")

sh = A.parse_plan_text("从上海出发去杭州玩3天")
check(sh.get("origin") == "上海市", "「从上海出发」认成出发地", str(sh))
check(sh.get("city") == "杭州市", "出发段先摘掉，目的地认的是杭州不是上海", str(sh))
check(A._parse_origin("随便找天出去玩")[0] is None, "没说出发地就不填，留着默认值")

# 真界面：大致想法里有地标 -> 开跑前把城市补进表单；已选目的地不被覆盖
atj = new_at()
atj.text_area(key="f_idea").set_value("嘉应大学附近好玩的地方").run()
next(b for b in atj.button if str(b.label) == "开始规划").click()
atj.run()
assert not atj.exception, f"地标自动填表抛异常：{atj.exception}"
check(atj.session_state["f_city"] == "梅州市", "面板里的城市真的补到了梅州", str(atj.session_state["f_city"]))
check(atj.session_state["f_province"] == "广东省", "省份跟着选中")
settle(atj)

atk = new_at()
atk.selectbox(key="f_province").set_value("北京市").run()
atk.selectbox(key="f_city").set_value("北京市").run()
atk.text_area(key="f_idea").set_value("嘉应大学附近好玩的地方").run()
next(b for b in atk.button if str(b.label) == "开始规划").click()
atk.run()
assert not atk.exception, f"已选目的地时抛异常：{atk.exception}"
check(atk.session_state["f_city"] == "北京市", "已经选好目的地时，文案不覆盖表单",
      str(atk.session_state["f_city"]))
settle(atk)

# 「从上海出发去杭州」：出发地和目的地都从这句话补进表单
atl = new_at()
atl.text_area(key="f_idea").set_value("从上海出发去杭州玩3天").run()
next(b for b in atl.button if str(b.label) == "开始规划").click()
atl.run()
assert not atl.exception, f"出发地自动填表抛异常：{atl.exception}"
check(atl.session_state["f_origin"] == "上海市" and atl.session_state["f_city"] == "杭州市",
      "出发地与目的地都能从大致想法补空",
      f"{atl.session_state.get('f_origin')}/{atl.session_state.get('f_city')}")
settle(atl)

# ---------------------------------------------------------------------------
# 3. 点卡片图片 -> 详情（要点 + 图集 + 视频）
# ---------------------------------------------------------------------------
at = new_at()
card = next((c for c in A.CITY_CARDS.get("成都市", [])), None)
check(card is not None, "成都示例卡片在数据里")
gallery = A._gallery("成都市", "四川省", card["img"])
check(len(gallery) >= 4 and len(set(gallery)) == len(gallery), "成都会凑出 4 张以上不重复的图集", str(len(gallery)))
check(all(u.startswith("https://images.unsplash.com/photo-") for u in gallery), "图集全是免版权直链")
video = A.VIDEO_LIBRARY.get("成都市")
check(video and video["bv"].startswith("BV"), "成都收录了真实 B 站视频", str(video)[:80])
check(all(str(v.get("bv", "")).startswith("BV") for v in A.VIDEO_LIBRARY.values()),
      "视频库里没有一个伪造的 BV 号")

btn_keys = [b.key for b in at.button]
detail_key = next((k for k in btn_keys if k.startswith("detail_")), "")
check(bool(detail_key), "每张卡片都有可点的图片按钮", str(btn_keys[:2]))
# AppTest 里 .run() 只是重跑脚本，真的要「点」得先 click()
at.button(key=detail_key).click()
at.run()
assert not at.exception, f"打开详情抛异常：{at.exception}"
body = texts(at)
labels = " ".join(str(b.label) for b in at.button)
# 只认弹窗正文里的特征串：样式表也含 dt-gal / dt-video，拿类名判断会误判通过
check("行程要点" in body, "详情弹窗里列出了行程要点")
check("<div class='dt-gal'>" in body and "unsplash.com/photo-" in body, "详情弹窗里有图集")
check("player.bilibili.com" in body or "search.bilibili.com" in body, "视频要么内嵌要么给搜索入口")
check("这个地方长什么样" in body, "图集有小标题")
check("就按这个行程规划" in labels and "只套用，我先改条件" in labels, "详情里两个动作按钮都在")
check("<div class='dt-tl'>" in body and "D1" in body, "详情左栏排了 Day 时间轴")
check("主要看点" in body and "这趟适合谁" in body, "详情右栏补了看点与适配两块")
check("换个地方也行" in body, "详情底部接上相关推荐，别留黑边")
check(any(str(b.key).startswith("rel_") for b in at.button), "相关推荐能直接点着跑")
check("日均预算约" in body, "右栏的预算数字是已有数据算出来的，不是编的")

# 「这趟适合谁」不能是套话：说的「全程 N 个主点」要和上方看点清单数得一样。
# 直接从弹窗正文里数，比猜用户点的是哪张卡更稳。
n_stops = body.count("class='dt-li'>")
fit_said = re.search(r"全程 (\d+) 个主点", body)
check(fit_said is not None and int(fit_said.group(1)) == n_stops and n_stops > 0,
      "适配文案的主点数和看点清单对得上", f"文案={fit_said.group(1) if fit_said else '无'} 清单={n_stops}")
check("不超过 3 个主点" not in body, "旧的写死「3 个主点」已经删掉")
check("你说的原话" not in body, "不再用「你说的原话」这种交差口吻")
# 首屏点卡进来时用户根本没说话，那句 request 是卡片自带文案，不能挂到用户头上
check("这条线的设定：" in body and "你说过：" not in body, "没开过口时把引用标成卡片设定")
check(body.count("class='dt-fit'>") == 2 and "class='dt-fit dt-fit-quote'" in body,
      "适配凑齐三行：人群 / 节奏 / 原话引用")
check("适合" in body and ("节奏偏满" in body or "节奏偏松" in body), "前两行确实是人群和节奏")

# 用户真的在「大致想法」里写过字之后，同一行要换成「你说过」并引用原句
chatted = new_at()
said = "带老人去成都，想去室内博物馆"
chatted.text_area(key="f_idea").set_value(said).run()
next(b for b in chatted.button if str(b.label) == "开始规划").click()
chatted.run()
assert not chatted.exception, f"写想法后开跑抛异常：{chatted.exception}"
chat_detail = next(str(b.key) for b in chatted.button if str(b.key).startswith("detail_"))
chatted.button(key=chat_detail).click()
chatted.run()
body2 = texts(chatted)
check("你说过：" in body2 and "这条线的设定" not in body2, "开过口之后才用「你说过」")
check(f"你说过：{said}" in body2, "引用的就是用户自己写的那句")

# 主点现在存在 CARD_STOPS 里，抠 desc 只是没登记时的兜底，所以断言换成三件事：
# 每张卡都登记过、条目不能是整句评价、详情列出来的就是登记的那些。
unregistered = [c["key"] for cards in A.CITY_CARDS.values() for c in cards if c["stops"] is None]
check(not unregistered, "每张卡都在 CARD_STOPS 里登记过主点", "、".join(unregistered[:4]))
bad_stop = [
    f"{c['key']}:{s}"
    for cards in A.CITY_CARDS.values()
    for c in cards
    for s in A._stops_of(c)
    if len(s) > 6 or re.match(r"^(?:想|尽量|别|也要|还有|每天|一次|一路)", s)
]
check(not bad_stop, "主点都是短词，没有整句评价或诉求", "、".join(bad_stop[:4]))
check(A._stops_of(A.CITY_CARDS["北京市"][1]) == [], "北京慢游确实没点名地方，走兜底文案")
opened_key = detail_key.removeprefix("detail_")
opened = next(c for c in A.CITY_CARDS[opened_key.rsplit("-", 1)[0]] if c["key"] == opened_key)
bullets = re.findall(r"<div class='dt-li'>· (.*?)</div>", body)
check(bullets == opened["stops"], "详情列的看点就是卡片登记的主点", f"{bullets} vs {opened['stops']}")

# 3a. 「看『xx』这条」只能换一张卡继续看详情，绝不能顺手把 AI 规划点了
#（旧版这里写的是 _run_preset，点一下背景变暗、顶部转圈、反复吐「已套用」，永远进不去详情）
# AppTest 会把弹窗元素留在树里，元素本身证明不了弹窗关没关。st 是和 app 共享的模块对象，
# 所以这里包住 st.dialog，直接数「这一轮到底开没开弹窗」。
opens: list[str] = []
_real_dialog = A.st.dialog


def _counting_dialog(*dargs: Any, **dkw: Any):
    deco = _real_dialog(*dargs, **dkw)

    def wrap(fn):
        inner = deco(fn)

        def call(*args: Any, **kw: Any):
            opens.append(str(A.st.session_state.get("_open_detail") or ""))
            return inner(*args, **kw)

        return call

    return wrap


A.st.dialog = _counting_dialog
try:
    rel_key = next(str(b.key) for b in at.button if str(b.key).startswith("rel_"))
    rel_target = rel_key.removeprefix("rel_")
    opened_before = len(opens)
    at.button(key=rel_key).click()
    at.run()
    assert not at.exception, f"点推荐位抛异常：{at.exception}"
    check(at.session_state.get("_open_detail") == rel_target, "点推荐位只把弹窗换到那张卡", str(at.session_state.get("_open_detail")))
    # 弹窗里的按钮被点时，那一轮必然先把旧弹窗打开一次才拿到 click，所以稳定后最后一次才是新卡
    check(opens[-1] == rel_target and opens.count(rel_target) == 1, "稳定后打开的是新那张卡", str(opens[-3:]))
    check(not at.error and at.session_state.get("_agent_job") is None,
          "点推荐位没有触发规划（离线缺密钥才会报错）", str([str(e.value)[:40] for e in at.error])[:80])
    body_rel = texts(at)
    check("已套用" not in body_rel, "点推荐位不再吐「已套用」")
    check("这趟适合谁" in body_rel, "换卡之后还是详情弹窗，不是回列表")
    switched = A.find_card(rel_target)
    check(switched is not None and str(switched["title"]) in body_rel, "弹窗正文确实是新那张卡", str(switched and switched["title"]))

    # 「返回」要真的关掉弹窗：清标记 + 那一轮不再调用 st.dialog
    opened_before = len(opens)
    at.button(key="detail_back").click()
    at.run()
    assert not at.exception, f"点返回抛异常：{at.exception}"
    check(not str(at.session_state.get("_open_detail") or ""), "点返回清空了打开标记", str(at.session_state.get("_open_detail")))
    check(len(opens) == opened_before + 1, "返回后不再重开详情，只多算拿到 click 那一轮", f"{len(opens)} vs {opened_before}")

    # 反过来：详情里点「就按这个行程规划」才应该开跑
    at.button(key=detail_key).click()
    at.run()
    assert not at.exception, f"重开详情抛异常：{at.exception}"
    check(opens[-1] == opened_key, "重新点卡片又能打开详情", str(opens[-3:]))
    opened_before = len(opens)
    at.button(key=f"go_{opened_key}").click()
    at.run()
    assert not at.exception, f"点规划抛异常：{at.exception}"
    # 规划挪进了后台线程：起任务那一轮可能已经跑完（离线只是缺密钥，几毫秒就返回），
    # 所以这里只认「任务起过了」，进度卡和取消出口在下面单独用假任务验。
    check(at.session_state.get("_agent_job") is not None or at.session_state.get("_last_payload") is not None,
          "只有『就按这个行程规划』会起规划任务")
    note = settle(at)
    check(at.session_state.get("_agent_job") is None, "任务结束后从 session_state 收干净")
    check(bool(note), "离线缺密钥时以一句人话收尾，而不是把异常抛在页面上", note[:60])
    check(len(opens) == opened_before + 1, "开跑后详情不再重开，只多算拿到 click 那一轮", f"{len(opens)} vs {opened_before}")
finally:
    A.st.dialog = _real_dialog


# ---------------------------------------------------------------------------
# 3a2. 规划进度卡：按真实阶段推进 + 「取消规划」真的掐断
# ---------------------------------------------------------------------------
# 塞一个「起了但没跑完」的假任务：线程压根没 start，卡片才会稳定停在 running 态
app_src = (ROOT / "app.py").read_text(encoding="utf-8")
atm = new_at()
job = A._PlanJob("成都三日游", {"city": "成都市", "province": "四川省", "days": 3})
atm.session_state["_agent_job"] = job
atm.run()
assert not atm.exception, f"进度卡那一轮抛异常：{atm.exception}"
plan_card = "\n".join(str(m.value) for m in atm.markdown)
check("ci-plan-card" in plan_card and "正在为您规划行程" in plan_card, "任务在跑时进度卡铺在页面上")
check("role='status'" in plan_card, "进度卡对读屏软件报 status，不用听整页")
check(plan_card.count("class='ci-plan-step ") == len(A.PLAN_STAGES),
      "卡上按阶段逐行列出，用户知道还剩哪几步", str(plan_card.count("class='ci-plan-step ")))
cancel_keys = [str(b.key) for b in atm.button if str(b.key).startswith("ci_plan_cancel")]
check(cancel_keys == [f"ci_plan_cancel_{job.run_id}"],
      "卡上留了「取消规划」出口，且 key 跟着这趟 run_id，不会撞上上一趟的残 button", str(cancel_keys))
check("data-ci-cancel='1'" in plan_card and "_embed_plan_ticker(job.run_id)" in app_src,
      "卡片里的视觉按钮由同源 iframe 转给那个真 st.button，点得动")
# 进度只认阶段，不掺时间因子：慢请求不该先把条跑满再干等
def fill_pct(done: set[int], elapsed: float) -> str:
    m = re.search(r"ci-plan-fill' style='width:(\d+)%", A._plan_progress_html(done, "", elapsed))
    return m.group(1) if m else ""


check(fill_pct(set(), 54.0) == fill_pct(set(), 0.0),
      "同一阶段下 54 秒和刚起步长得一样，不会「跑满卡住」", f"{fill_pct(set(), 54.0)}%")
check(fill_pct(set(range(len(A.PLAN_STAGES))), 54.0) == "100", "阶段全打勾才到 100%")

atm.button(key=f"ci_plan_cancel_{job.run_id}").click()
atm.run()
assert not atm.exception, f"点取消抛异常：{atm.exception}"
check(job.cancelled.is_set(), "点「取消规划」把取消标记递进后台线程")
check(atm.session_state.get("_agent_job") is None, "取消这一轮就交还主界面，不等后台那步跑完")
# 卡片真从页面上消失只能在浏览器里看：AppTest 会把上一轮的元素树留着
cancel_branch = app_src.split('cancel_key = f"ci_plan_cancel_{job.run_id}"')[1].split("if job.finished")[0]
check('st.session_state.pop("_agent_job", None)' in cancel_branch
      and "box.empty()" in cancel_branch and 'st.rerun(scope="app")' in cancel_branch,
      "取消这一支：交回任务 + 抹掉卡片 + 整页 rerun，不停在 fragment 里原地转圈",
      cancel_branch.replace("\n", " ")[:90])

# 后台线程晚到的收尾：被取消的这一趟既不该飘红，也不该把半成品写成结果
late = A._PlanJob("成都三日游", {"city": "成都市", "province": "四川省", "days": 3})
late.error = A.agent.AgentCancelled("用户已取消")
late.finished.set()
atm.session_state["_agent_job"] = late
atm.run()
assert not atm.exception, f"取消收尾那一轮抛异常：{atm.exception}"
check(atm.session_state.get("_agent_job") is None, "取消掉的任务收尾后从 session_state 收干净")
check(not atm.session_state.get("_plan_error") and not atm.error,
      "用户自己按的取消不当成报错，页面上不飘红")
check(not atm.session_state.get("result"), "取消的这趟没把半成品写成结果")

def markdown_hits(at: AppTest, needle: str, act: Any) -> int:
    """数「这一次运行里 app 真正发出了几条含 needle 的 markdown」。

    AppTest 会把弹窗元素留在元素树里，事后翻 at.markdown 判断不了某轮到底渲染了什么。
    st 是和 app 共享的模块对象，包一层就能拿到实时调用记录。
    """
    counter = [0]
    real = A.st.markdown

    def spy(text: Any, **kw: Any) -> Any:
        if needle in str(text):
            counter[0] += 1
        return real(text, **kw)

    A.st.markdown = spy
    try:
        act()
    finally:
        A.st.markdown = real
    assert not at.exception, f"操作抛异常：{at.exception}"
    return counter[0]


# ---------------------------------------------------------------------------
# 3c. 启程片头：航线描出 + 推镜放大 + 封面收场，必须可跳过、只播一次
# ---------------------------------------------------------------------------
no_xy = [str(c["payload"].get("city")) for cards in A.CITY_CARDS.values() for c in cards
         if str(c["payload"].get("city")) not in A.CITY_XY]
check(not no_xy, "每张卡的目的地都登记了坐标", "、".join(dict.fromkeys(no_xy))[:60])
out_box = [name for name, (lng, lat) in A.CITY_XY.items()
           if not (A._MAP_BOX[0] <= lng <= A._MAP_BOX[1] and A._MAP_BOX[2] <= lat <= A._MAP_BOX[3])]
check(not out_box, "坐标全落在示意图范围内，点不会画到画布外", "、".join(out_box[:4]))
check(A.ORIGIN_DEFAULT in A.regions.cities_of("北京市"), "默认出发地在联动数据里")
check(A.ORIGIN_DEFAULT in A.CITY_XY, "默认出发地有片头坐标")

intro = A._intro_html(card, "上海市")
check("dt-intro" in intro and "dt-route" in intro, "片头里有航线而不只是图片")
check("pathLength='1'" in intro, "描线用 pathLength=1，dash 单位和实际路径长度脱钩")
check("dt-intro-hero" in intro and str(card["img"]) in intro, "片头收在目的地封面上")
check("从上海出发 → 成都" in intro, "字幕写清了出发地到目的地", A.re.sub(r"<[^>]+>", " ", intro)[-60:].strip())
check("--dt-zoom-origin:" in intro, "推镜的缩放原点按目的地坐标算，不是写死居中")
check(A._intro_html({"payload": {"city": "不存在的城"}, "img": ""}, "北京市") == "",
      "查不到坐标就干脆不播，不能把点画歪")
# 出发地换成没坐标的地方时，航线段整段消失，但片头照播（目的地还是画得准）
noref = A._intro_html(card, "没听过的城")
check("dt-route" not in noref and "dt-dst" in noref, "出发地没坐标时退化成只标目的地")
same = A._intro_html(card, "成都市")
check("dt-route" not in same and "dt-src" not in same, "出发地=目的地时不画退化的零长航线")
check("成都就是出发地，落地直接开逛" in same, "同城时字幕不再自问自答", A.re.sub(r"<[^>]+>", " ", same)[-60:].strip())
check("°E" in same and "°N" in same, "经纬网带度数标注，看着是张有坐标的图")

src = (ROOT / "app.py").read_text(encoding="utf-8")
check("@keyframes dtRouteDraw" in src and "@keyframes dtIntroZoom" in src,
      "描线和推镜的时间轴还在 CSS 里（st.markdown 会吃掉 <script>，动画只能靠这套 keyframes）")
check(".st-key-intro_skip" in src, "跳过按钮的定位样式还在，不然会被片头盖住点不到")
# 线上 owner 视角里，Streamlit Cloud 外壳的「Manage app」按钮占掉底部 44px，
# 实测把 bottom:22px 的跳过按钮压住 101×22，所以离底必须留出那段高度。
skip_css = re.search(r"\.st-key-intro_skip \{([^}]*)\}", src)
skip_bottom = re.search(r"bottom:\s*(\d+)px", skip_css.group(1)) if skip_css else None
check(skip_bottom is not None and int(skip_bottom.group(1)) >= 56,
      "跳过按钮离底 ≥56px，不会被 Cloud 外壳的 Manage app 压住",
      f"bottom={skip_bottom.group(1) if skip_bottom else '无'}")

# 片头已改成 iframe 组件：Streamlit 只负责喂 payload / 收 intro:done，
# 播放、落位、跳过都在组件内部，不再产生 dt-intro / intro_skip 页面节点。
intro_result = {
    "constraints": {"city": "成都市", "days": 2, "people": 2},
    "itinerary": {"days": [{"day": 1, "items": [
        {"type": "attraction", "name": "宽窄巷子", "lng": 104.055, "lat": 30.669,
         "start": "09:00", "end": "11:30", "note": "老成都街巷",
         "photos": ["https://example.com/a.jpg"]},
        {"type": "meal", "name": "陈麻婆豆腐", "lng": 104.06, "lat": 30.67,
         "start": "12:00", "end": "13:00"},
    ]}]},
}
at_intro = new_at()
at_intro.session_state["result"] = intro_result
at_intro.session_state["_intro_pending"] = True
at_intro.run()
assert not at_intro.exception, at_intro.exception
intro_nodes = [e for e in at_intro
               if type(e).__name__ == "UnknownElement" and str(getattr(e, "key", "")) == "plan_intro"]
check(len(intro_nodes) == 1, "规划完成后片头以 plan_intro iframe 组件出现（单实例）", str(len(intro_nodes)))
check(not at_intro.session_state.get("_intro_pending"),
      "片头只播一次：_intro_pending 在本轮被消费", str(at_intro.session_state.get("_intro_pending")))
check(not any(str(b.key) == "intro_skip" for b in at_intro.button),
      "跳过 / 下一步都在 iframe 内，不再渲染成 Streamlit 按钮")

comp_src = (ROOT / "components/map_intro_component/index.html").read_text(encoding="utf-8")
check("#next-btn.is-on" in comp_src and "intro-landed" in comp_src,
      "落位后 next-btn 进入可点态（is-on + intro-landed）")
check("CI.host && CI.host.emit('intro:done', { reason: reason });" in comp_src,
      "下一步按钮 emit intro:done，而不是 Streamlit 按钮回调")

# 回传：拿到 intro:done 才写 _intro_result，下一轮据此离开片头
cap: dict = {}
_real_intro_component = A.map_intro
A.map_intro = lambda **kw: (cap.update(kw) or {"type": "intro:done", "reason": "next"})
try:
    A.st.session_state.clear()
    event = A._play_intro(intro_result)
    stored = A.st.session_state.get("_intro_result")
finally:
    A.map_intro = _real_intro_component
payload = cap.get("payload") or {}
check(cap.get("key") == "plan_intro" and payload.get("city") == "成都市"
      and payload.get("days") == 2 and payload.get("people") == 2,
      "iframe 拿到真实约束（成都 / 2 天 / 2 人）", str(cap.get("key")))
check(len(payload.get("pois") or []) == 1
      and payload["pois"][0]["name"] == "宽窄巷子"
      and payload["pois"][0]["photos"] == ["https://example.com/a.jpg"],
      "payload 只含 attraction（meal 被过滤），实拍图跟着点位进 iframe",
      str([p.get("name") for p in payload.get("pois") or []]))
check(event == {"type": "intro:done", "reason": "next"} and stored == event,
      "intro:done 回传后写进 _intro_result，下一轮才离开片头", str(stored))
# 详情页那份出发地用独立 key：面板摊开时两处同时渲染，重名会直接 DuplicateElementKey
at_d = new_at()
d_key = next(str(b.key) for b in at_d.button if str(b.key).startswith("detail_"))
at_d.button(key=d_key).click()
at_d.run()
assert not at_d.exception, f"打开详情弹窗抛异常：{at_d.exception}"
check(any(str(sb.key) == "f_origin" for sb in at_d.selectbox), "详情页里也能选出发地")

# 卡片正面必须自带「几天几晚 + 价格」，否则挑起来心里没底
face = A._card_face(card, 0)
check("天" in face and "晚" in face, "卡片正面写了几天几晚", re.sub(r"<[^>]+>", " ", face)[-90:].strip())
check("pc-price" in face and "¥" in face, "卡片正面写了价格")
check("一键生成这个行程" in labels, "套用按钮换成醒目的主 CTA 文案", labels[:60])

# ---------------------------------------------------------------------------
# 3b. 视频库没收录的地方也不能开天窗：给 B 站搜索入口
# ---------------------------------------------------------------------------
# 直接截住 st.markdown 看它到底拼出什么链接，比让 AppTest 跑一遍更准
seen: list[str] = []
_real_md = A.st.markdown
A.st.markdown = lambda text, **kw: seen.append(str(text))
try:
    A._video_box("不存在的城", {})
    A._video_box("成都市", A.VIDEO_LIBRARY["成都市"])
finally:
    A.st.markdown = _real_md
fallback, embed = seen[0], seen[1]
chengdu_bv = A.VIDEO_LIBRARY["成都市"]["bv"]
check("search.bilibili.com" in fallback and "不存在" in fallback,
      "没有 BV 号时退化成站内搜索链接", fallback[:90])
check(f"bvid={chengdu_bv}" in embed and "autoplay=0" in embed,
      "有 BV 号时内嵌播放器且默认不自动播", embed[:90])

# ---------------------------------------------------------------------------
# 4. 点「套用这个行程」-> 条件写回 + 立刻开跑（本机没密钥，必然报规划失败）
# ---------------------------------------------------------------------------
at = new_at()
apply_key = next(k for k in (b.key for b in at.button) if k.startswith("preset_"))
at.button(key=apply_key).click()
at.run()
assert not at.exception, f"app 抛异常：{at.exception}"
check(at.session_state["f_city"] not in ("", A.UNLIMITED_CITY),
      "套用后城市已回填", str(at.session_state["f_city"]))
note = settle(at)
check(bool(note), "套用后直接进了规划（离线清空密钥，报的是规划失败）", note[:60])
check("1-10" not in note and "请先填写目的地" not in note,
      "规划失败的原因是缺密钥，不是条件不合法", note[:120])
check("把「城市」选成具体城市" not in note, "不再是「请先选城市」那种没开跑的情况")
# 天数早就不走滑块了，卡片说的几天必须落到「出发 + 回程」这两个日期上
wanted_key = apply_key.split("preset_", 1)[1]
preset = next(p for group in A.CITY_CARDS.values() for p in group if p["key"] == wanted_key)
idea_text = str(at.session_state.get("f_idea") or "")
comps = A.plain_companions(list(preset["payload"].get("companions") or []))
check(bool(idea_text) and all(c in idea_text for c in comps)
      and str(preset["payload"].get("request") or "") in idea_text,
      "套用后卡片的人群 / 偏好 / 诉求合并进「大致想法」", idea_text[:50])
span = (at.session_state["f_end"] - at.session_state["f_start"]).days + 1
check(span == int(preset["payload"].get("days") or 0),
      f"套用「{preset['title']}」后天数为卡片说的 {preset['payload'].get('days')} 天", f"实际 {span} 天")
check(1 <= span <= 10, "套用后的天数落在 Agent 允许的 1-10 天内", str(span))

print("\nFAIL " + str(len(fails)) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
