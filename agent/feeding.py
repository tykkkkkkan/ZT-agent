"""
agent/feeding.py — 用饵量测算（任务书三·3「买多少够用」的落点）

为什么单独成模块
----------------
「一亩塘养鲤鱼要备多少饵」这类问题，如果交给大模型自由作答，数字没有出处 ——
答辩时老师一句「这个数怎么算的」就答不上来。所以把行业常规取值固化成表，
由代码算出总量，模型只负责转述：凡是有确定答案的数字，都不让模型编。
这跟 price / inventory 交给 pricing.py、services.py 是同一个原则。

算法（饵料系数法，渔业通行的估算口径）
--------------------------------------
    预计产量(kg) = 亩产(kg/亩) × 塘口面积(亩)
    总用饵量(kg) = 预计产量 × 饵料系数(FCR)
    包数         = 总用饵量(kg) ÷ 单包净含量(kg)

为什么用饵料系数而不是日投饵率
------------------------------
FCR 一个养殖周期就一个数，好记好验；日投饵率随水温、鱼体重逐日变化，
要天天称鱼才能算准，不适合一次性估算。表里的 daily_rate 只用于给
「分几次买」的建议，不参与总量计算。

表内取值说明
------------
均为池塘主养模式的常规参考值（行业资料与饲料厂推荐量的常见区间取中位数），
实际会随苗种规格、水温、溶氧、投喂管理上下浮动 15%~20%，
因此在输出里明确写出「按 亩产 × 亩数 × 饵料系数」的口径，便于按实调整。
"""

import math
import re
from typing import NamedTuple, Optional


# ──────────────────────────────────────────────────────────────
# 鱼种参数表
#   yield_kg_per_mu  池塘主养模式下的常规亩产（kg）
#   fcr              饵料系数（吃多少斤饵长一斤鱼）
#   cycle_days       从放苗到上市的常规养殖周期（天）
#   daily_rate       日投饵率区间（占存塘鱼体重的比例），仅用于分期建议
#   note             一句话说明，转述给客户时带上，避免把估算说成承诺
# ──────────────────────────────────────────────────────────────
class FeedingProfile(NamedTuple):
    yield_kg_per_mu: float
    fcr: float
    cycle_days: int
    daily_rate: tuple
    note: str


FEEDING_PROFILES = {
    "鲤鱼":   FeedingProfile(1000, 1.8, 180, (0.02, 0.03),
                          "鲤鱼主养塘，成鱼阶段日投饵率 2%~3%"),
    "鲫鱼":   FeedingProfile(800, 1.7, 180, (0.02, 0.03),
                          "鲫鱼精养塘，规格偏小时饵料系数略高"),
    "草鱼":   FeedingProfile(1200, 2.0, 200, (0.02, 0.04),
                          "草鱼吃食量大，配合饲料与青饲料搭配时系数可降"),
    "罗非鱼": FeedingProfile(1500, 1.6, 150, (0.03, 0.05),
                          "罗非鱼生长快、系数低，水温低于 18℃ 停食"),
    "青鱼":   FeedingProfile(900, 2.2, 200, (0.02, 0.03),
                          "青鱼以螺蛳等动物性饵料为主，配合饲料系数偏高"),
    "鲢鳙":   FeedingProfile(600, 1.2, 180, (0.01, 0.02),
                          "鲢鳙以滤食为主，主要靠肥水，精料只作补充"),
    "黄颡鱼": FeedingProfile(800, 1.5, 200, (0.02, 0.03),
                          "黄颡鱼全程配合饲料，苗期需破碎料"),
    "鲈鱼":   FeedingProfile(1000, 1.5, 180, (0.02, 0.04),
                          "加州鲈冰鲜与配合饲料过渡期系数波动较大"),
    "黑鱼":   FeedingProfile(1500, 1.9, 180, (0.03, 0.05),
                          "黑鱼肉食性，全程冰鲜或膨化料"),
}  # type: dict[str, FeedingProfile]

# 兜底：鱼种不在表里（如「混养塘」「四大家鱼」）时用它，避免直接拒答
DEFAULT_PROFILE = FeedingProfile(
    1000, 1.8, 180, (0.02, 0.03),
    "未收录该鱼种，按池塘主养的常规参数估算，实际请按当地养殖习惯调整",
)

DEFAULT_PACKAGE_GRAMS = 200     # 无法从产品规格解析时的单包净含量
PURCHASE_BATCH_DAYS = 15        # 建议的备货批次间隔（天）


def _norm(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def lookup_profile(fish: str):
    """按鱼种名取参数表条目，支持「鲤鱼主养塘」这类带修饰的说法。

    返回 (参数dict, 命中的键名)；未命中时返回 (DEFAULT_PROFILE, "")。
    """
    t = _norm(fish)
    if not t:
        return DEFAULT_PROFILE, ""
    # 先精确，再包含：避免「鲤鱼」被「鲤」以外的键抢先匹配
    for key, prof in FEEDING_PROFILES.items():
        if t == key:
            return prof, key
    for key, prof in FEEDING_PROFILES.items():
        if key in t:
            return prof, key
    # 俗称兼容：客户说「鲤鱼苗」「四大家鱼混养」时尽量落到最接近的一项
    aliases = {
        "鲤": "鲤鱼", "鲫": "鲫鱼", "草": "草鱼", "罗非": "罗非鱼",
        "青鱼": "青鱼", "胖头": "鲢鳙", "花鲢": "鲢鳙", "白鲢": "鲢鳙",
        "鳙": "鲢鳙", "黄骨": "黄颡鱼", "黄辣丁": "黄颡鱼", "加州鲈": "鲈鱼",
        "生鱼": "黑鱼", "乌鳢": "黑鱼",
    }
    for alias, key in aliases.items():
        if alias in t:
            return FEEDING_PROFILES[key], key
    return DEFAULT_PROFILE, ""


def package_grams(spec: str) -> int:
    """从产品规格里解析单包净含量（克）。

    规格在库里长这样：「200g/包，1箱=10包」「150g/包」「5kg/袋」。
    解析不出来就按 200g 兜底 —— 宁可给一个能算的近似值，也不要因为
    某个商品规格写得不规范就让整个测算失败。
    """
    s = _norm(spec).lower()
    if not s:
        return DEFAULT_PACKAGE_GRAMS
    m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|千克|公斤|g|克)", s)
    if not m:
        return DEFAULT_PACKAGE_GRAMS
    value = float(m.group(1))
    unit = m.group(2)
    grams = value * 1000 if unit in ("kg", "千克", "公斤") else value
    return int(round(grams)) if grams > 0 else DEFAULT_PACKAGE_GRAMS


ANGLE_PACKS_MIN = 1             # 钓友每次作钓每人用饵包数的常见下限
ANGLE_PACKS_MAX = 3             # 上限
ANGLE_PACKS_TYPICAL = 2         # 取中值做估算
PACKS_PER_BOX = 10              # 常规装箱：1 箱 = 10 包


def estimate_stock(fish: str, area_mu: float, days: Optional[int] = None,
                   package_grams_override: Optional[int] = None) -> dict:
    """养殖投喂：一个塘口在一个养殖周期内需要多少饵（公斤/吨级）。

    :param fish:     主养鱼种（可以是「鲤鱼」「鲤鱼主养塘」「四大家鱼」）
    :param area_mu:  塘口面积（亩）
    :param days:     养殖周期（天），不传就用表里的常规周期
    :param package_grams_override: 已知单包净含量（克）时直接指定，优先于默认值
    """
    profile, matched = lookup_profile(fish)
    try:
        area = float(area_mu)
    except (TypeError, ValueError):
        area = 0.0
    if area <= 0:
        area = 1.0
        area_assumed = True
    else:
        area_assumed = False

    cycle = int(days) if days and int(days) > 0 else profile.cycle_days
    yield_kg = profile.yield_kg_per_mu * area
    total_kg = yield_kg * profile.fcr
    pkg_g = package_grams_override or DEFAULT_PACKAGE_GRAMS
    bags = int(math.ceil(total_kg * 1000 / pkg_g)) if pkg_g else 0
    boxes = int(math.ceil(bags / PACKS_PER_BOX)) if bags else 0
    daily_kg = total_kg / cycle if cycle else 0
    batch_kg = daily_kg * PURCHASE_BATCH_DAYS
    batch_bags = int(math.ceil(batch_kg * 1000 / pkg_g)) if pkg_g else 0

    low, high = profile.daily_rate
    return {
        "mode": "养殖",
        "fish": fish or "",
        "matched": matched,
        "area_mu": area,
        "area_assumed": area_assumed,
        "cycle_days": cycle,
        "yield_kg_per_mu": profile.yield_kg_per_mu,
        "yield_kg": yield_kg,
        "fcr": profile.fcr,
        "total_kg": total_kg,
        "total_ton": total_kg / 1000,
        "package_grams": pkg_g,
        "bags": bags,
        "boxes": boxes,
        "daily_kg": daily_kg,
        "daily_rate": (low, high),
        "batch_days": PURCHASE_BATCH_DAYS,
        "batch_kg": batch_kg,
        "batch_bags": batch_bags,
        "note": profile.note,
    }


def estimate_angling(fish: str = "", trips: int = 1, people: int = 1,
                     package_grams_override: Optional[int] = None) -> dict:
    """钓友作钓：去几次、几个人，一共需要多少包。

    钓友问的「买多少够用」跟养殖户问的完全不是一回事 ——
    前者一次出钓一两包，后者一塘货论吨算。混成一个公式会算出
    「钓一次鱼要 45000 包」这种明显不成立的结果，所以分开算。
    """
    try:
        t = max(1, int(trips or 1))
    except (TypeError, ValueError):
        t = 1
    try:
        n = max(1, int(people or 1))
    except (TypeError, ValueError):
        n = 1
    bags = t * n * ANGLE_PACKS_TYPICAL
    return {
        "mode": "作钓",
        "fish": fish or "",
        "trips": t,
        "people": n,
        "bags": bags,
        "bag_range": (t * n * ANGLE_PACKS_MIN, t * n * ANGLE_PACKS_MAX),
        "package_grams": package_grams_override or DEFAULT_PACKAGE_GRAMS,
    }


def recommend(fish: str = "", area_mu: float = 0, days: Optional[int] = None,
              trips: int = 0, people: int = 1,
              package_grams_override: Optional[int] = None) -> dict:
    """按场景分派：给了塘口面积就算养殖投喂，否则按钓友作钓算。

    为什么不默认都走养殖：「中渔天下」卖的是小包装饵料，
    大部分提问的人是钓友；只有明确提到塘口、亩数、投喂才按养殖算。
    """
    try:
        area = float(area_mu or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area > 0:
        return estimate_stock(fish, area, days, package_grams_override)
    if trips and int(trips) > 0:
        return estimate_angling(fish, trips, people, package_grams_override)
    # 什么都没给：先按 1 次出钓给参照值（这家的客人多数是钓友），
    # 不要按 1 亩养殖塘算 —— 那会得出「买 6800 包」这种一看就不对的结果。
    return estimate_angling(fish, 1, people, package_grams_override)


def explain(result: dict) -> str:
    """把估算结果写成一段可直接转述给客户的中文依据。

    单独一个函数：AI 工具层与后台/论文示例都调用它，
    保证「同一个数在对话里和文档里说法一致」。
    """
    if not result:
        return ""
    if result.get("mode") == "作钓":
        return _explain_angling(result)
    return _explain_stock(result)


def _explain_stock(r: dict) -> str:
    area_txt = f"{r['area_mu']:g} 亩"
    if r.get("area_assumed"):
        area_txt += "（未给面积，按 1 亩估算）"
    low, high = r["daily_rate"]
    ton = r["total_ton"]
    ton_txt = f"{ton:.2f} 吨" if ton >= 1 else f"{r['total_kg']:g} kg"
    return (
        f"按 {r['fish'] or '常规鱼种'}、{area_txt}、{r['cycle_days']} 天一个养殖周期估算：\n"
        f"亩产按 {r['yield_kg_per_mu']:g} kg，预计产鱼 {r['yield_kg']:g} kg；\n"
        f"饵料系数取 {r['fcr']}，全周期需饵约 {r['total_kg']:g} kg（{ton_txt}），"
        f"按每包 {r['package_grams']} g 折合约 {r['bags']} 包、{r['boxes']} 箱；\n"
        f"日均用量约 {r['daily_kg']:.2f} kg，日投饵率参考 {low*100:g}%~{high*100:g}%；\n"
        f"建议每次备 {r['batch_days']} 天左右的量（约 {r['batch_kg']:.1f} kg），"
        f"既够周转又不至于压库存。\n"
        f"说明：{r['note']}；实际用量会随水温、溶氧和投喂管理浮动，可按上面的口径自己折算。"
    )


def _explain_angling(r: dict) -> str:
    low, high = r["bag_range"]
    return (
        f"按 {r['trips']} 次作钓、每次 {r['people']} 人算：\n"
        f"常规每次每人备 {ANGLE_PACKS_TYPICAL} 包（打窝 + 钓饵），"
        f"合计约 {r['bags']} 包；实际用量的常见区间是 {low}~{high} 包。\n"
        f"野钓小水面鱼口慢，{low} 包就够；黑坑抢鱼或打重窝，按上限备更稳。\n"
        f"第一次用的话建议先拿 {r['bags']} 包试口，鱼情好再补，别一次囤太多——"
        f"饵料放久了香味会跑。"
    )
