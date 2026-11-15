"""离线校验详情与卡片数据：问答式解析、点图片出详情、套用卡片直接出行程。

用法：在仓库根目录跑 `python tools/chat_test.py`（AppTest 需要 cwd 是仓库根，
否则 app.py 里的相对路径会指错地方）。

这里会把密钥临时清空，所以「出现规划报错」正好用来证明流程真的开跑了，
而不是只回填了条件——否则每次跑测试都会真的调用一次大模型。
"""
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
# 相对日期不能倒挂：说过的日子要顺延到明年
late = A.parse_plan_text("1月5日去北京")["start"]
check(late > date.today(), "过去的日期顺延到下一年", str(late))

# ---------------------------------------------------------------------------
# 2. 问答式输入卡：说一句话 -> 条件写回 + 管家复述（侧栏已下线，改吸底 composer）
# ---------------------------------------------------------------------------
at = new_at()
check(not at.sidebar, "侧栏真的没了，页面只剩一条主区")
check("我是你的旅行管家" in texts(at), "输入卡上有管家标题")
check("请用大白话描述你的出行计划" in texts(at), "管家只说一句副标题")
check("直接用大白话说就行" not in texts(at), "示例文案不再重复第二遍（副标题里没有「比如」）")
check(any(sb.key == "f_country" for sb in at.selectbox), "『精确设置』里的级联下拉照样渲染（收起也在）")
check(any(str(b.label) == "开始规划" for b in at.button), "『开始规划』还在，只是搬进了输入卡")

faces = [m.value for m in at.markdown if "pc-face-" in str(m.value)]
check(len([b for b in at.button if str(b.key).startswith("preset_")]) == A.CARD_SLOTS,
      f"首屏铺满 {A.CARD_SLOTS} 张卡", str(len([b for b in at.button if str(b.key).startswith('preset_')])) )
grid = [m.value for m in at.markdown if "stHorizontalBlock" in str(m.value)]
check(bool(grid), "网格列数交给 CSS（auto-fit 规则已注入）")

# 冷启动闪烁的根因是「卡片 CSS 出现得太晚」：脚本从 hero 到卡片中间要跑一千多毫秒，
# 等 background-image 生效才发请求，12 张封面就空着那一秒多。preload 必须挂在最早的块里。
idx_hero = next((i for i, m in enumerate(at.markdown) if "hero-title" in str(m.value)), -1)
idx_css = next((i for i, m in enumerate(at.markdown) if ".pc-face-0" in str(m.value)), -1)
hero_md = str(at.markdown[idx_hero].value) if idx_hero >= 0 else ""
css_md = str(at.markdown[idx_css].value) if idx_css >= 0 else ""
pre = [unescape(u) for u in re.findall(r'<link rel="preload" as="image" href="([^"]+)"', hero_md)]
need = re.findall(r"background-image: url\('([^']+)'\)", css_md)
check(0 <= idx_hero < idx_css, "preload 比卡片 CSS 更早发出", f"hero={idx_hero} css={idx_css}")
check(len(pre) == A.CARD_SLOTS and set(pre) == set(need),
      f"首屏 {A.CARD_SLOTS} 张封面全部预加载", f"preload={len(pre)} 需要={len(need)}")

at.session_state["_pending_chat"] = "10月15号和老婆去扬州玩3天，人均2000，想看园林吃淮扬菜"
at.run()
assert not at.exception, f"app 抛异常：{at.exception}"
check(at.session_state["f_city"] == "扬州市", "说城市后侧栏落到扬州市", str(at.session_state["f_city"]))
check(at.session_state["f_province"] == "江苏省", "省份跟着反查出来", str(at.session_state["f_province"]))
check(at.session_state["f_budget"] == 4000, "人均 2000 × 2 人 = 总预算 4000", str(at.session_state["f_budget"]))
check(at.session_state["f_prefs_3"] is True, "「吃淮扬菜」勾上了美食打卡")
check(at.session_state["f_companions_1"] is True, "「和老婆」勾上了情侣/夫妻")
check("扬州市" in texts(at) and "还差点信息" not in texts(at), "管家复述里点名了城市")
check(at.session_state["f_request"].startswith("10月15号"), "用户原话进了行程诉求", str(at.session_state["f_request"])[:40])
scope = re.search(r'class="tv-bg-tag" data-scope="([^"]+)"', texts(at))
check(scope and scope.group(1) == "扬州市", "说完城市背景立刻换到扬州", scope.group(1) if scope else "无")

at.session_state["_pending_chat"] = "改成5天，带上爸妈"
at.run()
assert not at.exception, f"app 抛异常：{at.exception}"
end = at.session_state["f_end"]
start = at.session_state["f_start"]
check(isinstance(start, date) and isinstance(end, date) and (end - start).days == 4,
      "「改成5天」只动天数不动日期", f"{start}~{end}")
check(at.session_state["f_companions_2"] is True, "「带上爸妈」追加了带老人，没挤掉情侣/夫妻")
check(at.session_state["f_city"] == "扬州市", "增量修改不会把已定的城市清空")

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

# 用户真的说过话之后，同一行要换成「你说过」并引用他打的原话
chatted = new_at()
chatted.session_state["_pending_chat"] = "10月15号和老婆去扬州玩3天"
chatted.run()
assert not chatted.exception, f"聊天后打开详情抛异常：{chatted.exception}"
chat_detail = next(str(b.key) for b in chatted.button if str(b.key).startswith("detail_"))
chatted.button(key=chat_detail).click()
chatted.run()
body2 = texts(chatted)
check("你说过：" in body2 and "这条线的设定" not in body2, "开过口之后才用「你说过」")
# 原话在聊天气泡里也出现，所以必须贴着「你说过：」匹配才算真的引用对了
check("你说过：10月15号和老婆去扬州玩3天" in body2, "引用的就是用户自己打的那句")

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
    check(not at.error, "点推荐位没有触发规划（离线缺密钥才会报错）", str([str(e.value)[:40] for e in at.error])[:80])
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
    check(len(at.error) > 0, "只有『就按这个行程规划』会开跑", str(len(at.error)))
    check(len(opens) == opened_before + 1, "开跑后详情不再重开，只多算拿到 click 那一轮", f"{len(opens)} vs {opened_before}")
finally:
    A.st.dialog = _real_dialog

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
check(all(name in A.CITY_XY for name in A.ORIGIN_CITIES), "出发地候选全都有坐标",
      "、".join(n for n in A.ORIGIN_CITIES if n not in A.CITY_XY)[:40])
check(A.ORIGIN_DEFAULT in A.ORIGIN_CITIES, "默认出发地在候选里")

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

at3 = new_at()
d3 = next(str(b.key) for b in at3.button if str(b.key).startswith("detail_"))
played = markdown_hits(at3, "<div class='dt-intro'", lambda: (at3.button(key=d3).click(), at3.run()))
check(played == 1, "第一次打开详情只播一次启程片头", str(played))
check("dt-intro" in texts(at3), "片头确实进了页面")
check(any(str(b.key) == "intro_skip" for b in at3.button), "片头期间有「跳过片头」按钮")
check(any(str(b.key) == "f_origin" for b in at3.selectbox), "详情页里能选出发地")
again = markdown_hits(at3, "<div class='dt-intro'", lambda: at3.run())
check(again == 0, "重跑一轮后片头不再挡正文（每张卡只播一次）", str(again))

# 跳过之后弹窗还得开着，不能把详情一起关了
at4 = new_at()
d4 = next(str(b.key) for b in at4.button if str(b.key).startswith("detail_"))
markdown_hits(at4, "<div class='dt-intro'", lambda: (at4.button(key=d4).click(), at4.run()))
skipped = markdown_hits(at4, "<div class='dt-intro'", lambda: (at4.button(key="intro_skip").click(), at4.run()))
check(skipped == 0, "点跳过片头不再重播", str(skipped))
check("这趟适合谁" in texts(at4), "跳过之后详情正文照常开着")
# 换出发地要重播：航线得跟着新的出发城市重画
replayed = markdown_hits(at4, "<div class='dt-intro'", lambda: at4.selectbox(key="f_origin").set_value("乌鲁木齐市").run())
check(replayed == 1, "换出发地会重播片头", str(replayed))
check("从乌鲁木齐出发" in texts(at4), "重播的航线按新出发地画", str(at4.session_state.get("_intro_seen")))

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
check(len(at.error) > 0, "套用后直接进了规划（离线清空密钥，报的是规划失败）", str(len(at.error)))
check(not any("1-10" in str(e.value) or "请先填写目的地" in str(e.value) for e in at.error),
      "规划失败的原因是缺密钥，不是条件不合法",
      "; ".join(str(e.value)[:60] for e in at.error)[:120])
check(not any("把「城市」选成具体城市" in str(w.value) for w in at.warning),
      "不再是「请先选城市」那种没开跑的情况")
prefs = [i for i in range(len(A.PREFERENCE_OPTIONS)) if at.session_state.get(f"f_prefs_{i}")]
check(len(prefs) > 0, "套用后卡片里的偏好真的勾上了", str(prefs))
# 天数早就不走滑块了，卡片说的几天必须落到「出发 + 回程」这两个日期上
wanted_key = apply_key.split("preset_", 1)[1]
preset = next(p for group in A.CITY_CARDS.values() for p in group if p["key"] == wanted_key)
span = (at.session_state["f_end"] - at.session_state["f_start"]).days + 1
check(span == int(preset["payload"].get("days") or 0),
      f"套用「{preset['title']}」后天数为卡片说的 {preset['payload'].get('days')} 天", f"实际 {span} 天")
check(1 <= span <= 10, "套用后的天数落在 Agent 允许的 1-10 天内", str(span))

print("\nFAIL " + str(len(fails)) if fails else "\n全部通过")
sys.exit(1 if fails else 0)
