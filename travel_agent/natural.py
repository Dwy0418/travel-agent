"""「大致想法」的统一自然语言提取：人数、天数、预算、人群、偏好、避雷、硬需求。

前端解析与后端 build_constraints 都调用这里，避免两套正则各说各话。
只返回文本里确实说到的项；识别不到时返回 None / 空列表，调用方保持原值。
"""
from __future__ import annotations

import re

_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM = r"(\d{1,4}|[一二两三四五六七八九十]{1,3})"

#: 人物关系 -> (纯文本同行人标签, 隐含人数)
_COMPANION_RULES = [
    (("独自", "一个人", "自己一个", "单人", "solo"), "独自出行", 1),
    (("情侣", "老婆", "老公", "爱人", "夫妻", "两口子", "老两口", "小两口", "蜜月"), "情侣/夫妻", 2),
    (("爸妈", "父母", "二老", "双亲"), "带老人", 3),
    (("老人", "长辈", "爷爷", "奶奶", "外公", "外婆"), "带老人", None),
    (("小孩", "孩子", "宝宝", "娃", "儿童"), "带小孩", None),
    (("亲子", "一家三口", "一家四口", "全家", "一家人"), "亲子家庭", None),
    (("朋友", "同学", "伙伴", "闺蜜", "哥们"), "朋友结伴", None),
    (("团建", "同事"), "公司团建", None),
    (("宠物", "狗", "猫"), "携带宠物", None),
    (("孕妇", "怀孕"), "孕妇", None),
    (("轮椅", "无障碍", "行动不便"), "需要无障碍设施", None),
]

#: 正向偏好 -> 纯文本偏好标签
_PREF_RULES = [
    (("少走路", "不想太累", "别太累", "太累", "轻松", "休闲", "慢一点", "节奏慢", "不赶", "躺平", "度假"),
     "少走路/休闲游"),
    (("室内", "博物馆", "美术馆", "展览", "展馆", "科技馆"), "室内为主"),
    (("拍照", "出片", "打卡照", "摄影"), "出片率高"),
    (("美食", "好吃", "吃货", "小吃", "餐厅", "夜宵", "烤鸭", "火锅", "烧烤", "螺蛳粉"), "美食打卡"),
    (("户外", "自然", "徒步", "爬山", "登山", "露营"), "户外自然"),
    (("特种兵", "拉练", "暴走", "一天玩", "赶场"), "特种兵拉练"),
    (("购物", "逛街", "买买买", "商场"), "购物逛街"),
    (("文化", "历史", "古迹", "故宫", "古镇", "园林", "寺庙"), "文化历史"),
]

#: 避雷 / 反向需求
_AVOID_RULES = [
    (("不爬山", "不想爬山", "别爬山", "不登山", "不想登山", "不要爬山", "爬不动"), "不爬山"),
    (("人多", "拥挤", "排队", "人挤人", "避开人流", "不想挤"), "避开人流"),
    (("别太累", "不想太累", "太累", "不想赶", "别赶", "节奏慢", "轻松点"), "行程别太满"),
    (("不购物", "不想购物", "别逛街"), "不购物"),
]

#: 硬需求 -> 纯文本标签（会被 validator 硬校验）
_HARD_RULES = [
    (("宠物", "狗", "猫", "毛孩子"), "宠物友好"),
    (("轮椅", "无障碍", "行动不便"), "无障碍"),
    (("孕妇", "怀孕"), "孕妇友好"),
]

_FOOD_WORDS = ("烤鸭", "火锅", "烧烤", "螺蛳粉", "小吃", "早茶", "海鲜", "牛肉面", "羊肉泡馍", "小笼包")
_STYLE_WORDS = ("园林", "博物馆", "古镇", "寺庙", "胡同", "夜景", "海边", "雪山", "草原")

_PEOPLE_VAGUE = (
    "一家人", "全家", "一家子", "一家老小", "几口人", "几个人", "几个朋友",
    "朋友几个", "我们一家", "带家人", "带上家人", "家人一起", "带老人",
)


def _cn_int(token: str) -> int | None:
    token = str(token or "").strip()
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        return _CN_DIGITS.get(head, 1) * 10 + (_CN_DIGITS.get(tail, 0) if tail else 0)
    return _CN_DIGITS.get(token)


def _money(token: str, unit: str) -> float | None:
    value = _cn_int(token)
    if value is None:
        return None
    factor = {"万": 10000, "w": 10000, "W": 10000, "千": 1000, "k": 1000, "K": 1000}.get(unit, 1)
    return float(value) * factor


def extract_people(text: str) -> tuple[int | None, str]:
    """人数：先认明确总数，再认人物关系，最后才认「N 人」泛化说法。"""
    raw = str(text or "")
    if not raw:
        return None, ""

    m = re.search(rf"(?:一共|总共|共计|合计|共)\s*{_NUM}\s*[个位]?\s*人", raw)
    if m and (n := _cn_int(m.group(1))):
        return max(1, n), "明确总数"

    m = re.search(
        rf"{_NUM}\s*个?\s*(?:大人|成人|成年人)[^，。；,;]*?{_NUM}\s*个?\s*(?:小孩|孩子|儿童|小朋友)",
        raw,
    )
    if m and (a := _cn_int(m.group(1))) and (b := _cn_int(m.group(2))):
        return max(1, a + b), "大人+小孩"
    m = re.search(rf"{_NUM}\s*大\s*{_NUM}\s*小", raw)
    if m and (a := _cn_int(m.group(1))) and (b := _cn_int(m.group(2))):
        return max(1, a + b), "大人+小孩"

    m = re.search(rf"(?:夫妻俩?|两口子|我们俩)[^，。；,;]*?{_NUM}\s*个?\s*(?:小孩|孩子|儿童)", raw)
    if m and (kids := _cn_int(m.group(1))):
        return 2 + kids, "夫妻+小孩"
    m = re.search(rf"(?:一个人|独自|单独)[^，。；,;]*?{_NUM}\s*个?\s*(?:小孩|孩子|儿童)", raw)
    if m and (kids := _cn_int(m.group(1))):
        return 1 + kids, "一人+小孩"

    m = re.search(rf"一家\s*{_NUM}\s*口", raw)
    if m and (n := _cn_int(m.group(1))):
        return max(1, n), "一家N口"
    m = re.search(rf"{_NUM}\s*口之家", raw)
    if m and (n := _cn_int(m.group(1))):
        return max(1, n), "N口之家"
    m = re.search(rf"(?:全家|一家)\s*{_NUM}\s*人", raw)
    if m and (n := _cn_int(m.group(1))) and n > 1:
        return n, "一家N人"

    if re.search(r"夫妻俩?|两口子|老两口|小两口", raw):
        return 2, "夫妻两口"
    if re.search(r"(?:我们|咱们|咱|我俩)?\s*仨", raw):
        return 3, "仨"
    if re.search(r"独自|单独|一个人|自己一个|单人|solo", raw, re.I):
        return 1, "独自一人"
    if re.search(r"爸妈|父母|二老|双亲", raw):
        return 3, "带爸妈"
    if re.search(r"情侣|老婆|老公|爱人|蜜月", raw):
        return 2, "情侣/夫妻"

    m = re.search(rf"{_NUM}\s*(?:个|位)?\s*(?:朋友|同学|伙伴|同事)", raw)
    if m and (n := _cn_int(m.group(1))):
        return max(1, n), "N个朋友"
    m = re.search(rf"{_NUM}\s*(?:个)?\s*(?:人(?!均)|位)", raw)
    if m and (n := _cn_int(m.group(1))):
        before = raw[max(0, m.start() - 1):m.start()]
        if before != "家" and "一家人" not in m.group(0):
            return max(1, n), "N人"
    return None, ""


def vague_people(text: str) -> bool:
    raw = str(text or "")
    return bool(raw) and extract_people(raw)[0] is None and any(w in raw for w in _PEOPLE_VAGUE)


def extract_days(text: str) -> int | None:
    raw = str(text or "")
    m = re.search(rf"{_NUM}\s*(?:天|晚|日游)", raw)
    if m and (n := _cn_int(m.group(1))):
        return max(1, n)
    if "一日游" in raw:
        return 1
    if re.search(r"周末(?:去|玩|游|两天)?", raw):
        return 2
    return None


def extract_budget(text: str) -> dict[str, float | None]:
    raw = str(text or "")
    per = None
    m = re.search(rf"(?:人均|每人|人均预算)\s*{_NUM}\s*(万|千|w|k)?", raw)
    if m:
        per = _money(m.group(1), m.group(2) or "")
    total = None
    m = re.search(
        rf"(?:预算|花费|费用|总预算)?\s*(?:不超过|控制在|大概|约)?\s*{_NUM}\s*(万|千|w|k)?\s*(?:元|块|块钱)",
        raw,
    )
    if m:
        total = _money(m.group(1), m.group(2) or "")
    m = re.search(rf"(?:预算|总预算)\s*{_NUM}\s*(万|千|w|k)?", raw)
    if m and total is None:
        total = _money(m.group(1), m.group(2) or "")
    return {"per_person": per, "budget": total}


def _negated(raw: str, index: int) -> bool:
    head = raw[max(0, index - 4):index]
    return any(w in head for w in ("不", "别", "讨厌", "避免", "不要", "不想"))


def _picked(raw: str, rules) -> list[str]:
    out: list[str] = []
    for keys, label in rules:
        for key in keys:
            pos = raw.find(key)
            if pos >= 0 and not _negated(raw, pos):
                if label not in out:
                    out.append(label)
                break
    return out


def extract_companions(text: str) -> list[str]:
    raw = str(text or "")
    out: list[str] = []
    for keys, label, _ in _COMPANION_RULES:
        if any(k in raw for k in keys) and label not in out:
            out.append(label)
    return out


def extract_preferences(text: str) -> list[str]:
    return _picked(str(text or ""), _PREF_RULES)


def extract_avoid(text: str) -> list[str]:
    raw = str(text or "")
    out: list[str] = []
    for keys, label in _AVOID_RULES:
        if any(k in raw for k in keys) and label not in out:
            out.append(label)
    return out


def extract_hard(text: str) -> list[str]:
    raw = str(text or "")
    out: list[str] = []
    for keys, label in _HARD_RULES:
        if any(k in raw for k in keys) and label not in out:
            out.append(label)
    return out


def extract_foods(text: str) -> list[str]:
    raw = str(text or "")
    return [w for w in _FOOD_WORDS if w in raw]


def extract_styles(text: str) -> list[str]:
    raw = str(text or "")
    return [w for w in _STYLE_WORDS if w in raw]


def extract_all(text: str) -> dict:
    """一次性抽取全部维度；空值用 None / []，方便调用方只覆盖说到的项。"""
    raw = str(text or "")
    people, people_hit = extract_people(raw)
    companions = extract_companions(raw)
    prefs = extract_preferences(raw)
    # 带老人默认叠加少走路/休闲游：这是人物关系带来的硬性节奏要求
    if "带老人" in companions and "少走路/休闲游" not in prefs:
        prefs.append("少走路/休闲游")
    budget = extract_budget(raw)
    return {
        "people": people,
        "people_hit": people_hit,
        "vague_people": vague_people(raw),
        "companions": companions,
        "preferences": prefs,
        "avoid": extract_avoid(raw),
        "hard": extract_hard(raw),
        "foods": extract_foods(raw),
        "styles": extract_styles(raw),
        "days": extract_days(raw),
        "budget": budget["budget"],
        "per_person": budget["per_person"],
    }
