"""离线校验详情与卡片数据：问答式解析、点图片出详情、套用卡片直接出行程。

用法：在仓库根目录跑 `python tools/chat_test.py`（AppTest 需要 cwd 是仓库根，
否则 app.py 里的相对路径会指错地方）。

这里会把密钥临时清空，所以「出现规划报错」正好用来证明流程真的开跑了，
而不是只回填了条件——否则每次跑测试都会真的调用一次大模型。
"""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

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
