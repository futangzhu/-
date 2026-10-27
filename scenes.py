"""配图：调用生图插件的通用接口，画"她的小屋"。

2026-10-26 04:46 主人指出：「除了时间，现在提示词都一样」。
确实——四个时段原本是同一张照片换了光，每天配图会长得一模一样。
改成：**每个时段三个变体，按日期轮换**。同一天固定（不会刷一次变一张），跨天会变。

三条硬规矩（主人 03:07 定）：只拍风景静物、不出现人物、画面内不出现任何文字。
"""

from __future__ import annotations

import datetime
import logging

from . import mood, store

LOG = logging.getLogger("astrbot")

# 每个时段三个变体：换机位、换小物、换光的方向。屋子还是那间屋子。
SCENES: dict[str, dict] = {
    "dawn": {
        "hours": (5, 8),
        "variants": [
            "清晨的小屋，低角度暖光爬过木地板，窗边旧木桌上摊着一本日记，纸面空白看不清，"
            "毛毯搭在椅背上，杯子冒热气，窗台上一只旧不锈钢盆",
            "清晨的小屋，光从窗帘缝里漏进来，床沿搭着一条揉皱的毛毯，"
            "地板上一双拖鞋，桌上半杯凉掉的水，窗台上扣着旧不锈钢盆",
            "清晨的小屋，从窗边往屋里看，木桌一角摊着日记，纸页翻起，"
            "旧不锈钢盆里还留着一点水痕，空气里有雾",
        ],
    },
    "noon": {
        "hours": (8, 14),
        "variants": [
            "午后的小屋，阳光斜斜落在地板上，桌上一本摊开的日记字迹模糊，"
            "旁边一杯热茶、一盆绿植，旧不锈钢盆靠墙放着",
            "午后的小屋，从桌角看过去，窗子半开，风把日记的一页吹起，"
            "椅背上搭着毛毯，地板上有一小块光斑",
            "午后的小屋，光线很足，桌上摊着日记和一摞旧纸，"
            "杯子放在窗台上，旧不锈钢盆倒扣着当镇纸",
        ],
    },
    "dusk": {
        "hours": (14, 20),
        "variants": [
            "傍晚的小屋，暖橙色的夕阳铺满木桌，窗台上摊着一本日记，"
            "纸页被余晖照亮但一个字也看不清，毛毯搭在椅背，杯口还有热气",
            "傍晚的小屋，夕阳把窗框的影子拉得很长，桌上摊着日记，"
            "旧不锈钢盆泛着一点光，地板上散着几本书",
            "傍晚的小屋，从门口往里看，屋里已经有点暗，只有窗边那块还亮着，"
            "日记摊在光里，椅子被推开了一半",
        ],
    },
    "night": {
        "hours": (20, 29),   # 20 点到次日 5 点
        "variants": [
            "夜里的小屋，一盏暖黄的小灯亮着，灯下摊开的笔记本字迹柔和模糊，"
            "椅背上搭着毛毯，桌上杯子还温着，旧不锈钢盆在光线边缘",
            "夜里的小屋，只开了桌灯，光圈之外都是暗的，"
            "日记摊在灯下，窗外是深蓝的夜色，玻璃上有一层水汽",
            "夜里的小屋，床头一盏小灯，被子掀开一角，"
            "桌上有日记和一杯水，灯下的影子很长",
        ],
    },
}

FALLBACK = "dusk"
TAIL = "，温馨的生活感，摄影质感，静物，画面元素完整，不出现人物，画面内没有任何文字"


def _in_slot(hour: int, key: str) -> bool:
    lo, hi = SCENES[key]["hours"]
    return lo <= hour < hi if hi <= 24 else (hour >= lo or hour < hi % 24)


def slot(now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(store.TZ)
    for key in SCENES:
        if _in_slot(now.hour, key):
            return key
    return FALLBACK


def _pick_variant(key: str, day: str, variant: int | None = None) -> int:
    """按日期轮换。同一天稳定，跨天会换——不然每天的配图都是同一张。"""
    n = len(SCENES[key]["variants"])
    if variant is not None:
        return variant % n
    return (sum(ord(c) for c in day) + len(day)) % n


def build_prompt(now: datetime.datetime | None = None, force_key: str = "",
                 variant: int | None = None,
                 day: str | None = None) -> tuple[str, str]:
    """返回 (场景名, 完整提示词)。心情差就给阴一点的修饰。"""
    now = now or datetime.datetime.now(store.TZ)
    key = force_key if force_key in SCENES else slot(now)
    day = day or store.today_local()
    idx = _pick_variant(key, day, variant)
    base = SCENES[key]["variants"][idx]

    try:
        s = mood.score(now)["score"]
    except Exception:  # noqa: BLE001
        s = 0.55
    if s < 0.35:
        base += "，光线柔和偏暗，屋里很安静，暖调里带一点闷"
    elif s > 0.75:
        base += "，光线明亮通透，暖意更足"
    return key, base + TAIL


def _client(context):
    """取生图插件的通用接口。取不到返回 None。"""
    try:
        meta = context.get_registered_star("astrbot_plugin_image_generation")
        inst = getattr(meta, "star_cls", None) if meta else None
        return getattr(inst, "public_api", None)
    except Exception as e:  # noqa: BLE001
        LOG.warning(f"[life_memory] 取不到生图插件：{e}")
        return None


async def make(context, now: datetime.datetime | None = None,
               aspect: str = "4:3", force_key: str = "",
               variant: int | None = None) -> tuple[str, str]:
    """生成一张配图。返回 (本地路径, 场景名)；失败返回 ("", "")。"""
    client = _client(context)
    if client is None:
        return "", ""
    key, prompt = build_prompt(now, force_key, variant)
    try:
        result = await client.generate_image_files(
            prompt=prompt, source="life_memory", image_count=1,
            aspect_ratio=aspect, is_admin=True,
        )
    except Exception as e:  # noqa: BLE001
        LOG.warning(f"[life_memory] 生图失败：{e}")
        return "", ""

    paths = getattr(result, "paths", None) or []
    if not getattr(result, "ok", False) or not paths:
        LOG.warning(
            f"[life_memory] 生图未返回路径 ok={getattr(result,'ok',None)} "
            f"code={getattr(result,'code','')} msg={getattr(result,'message','')}"
        )
        return "", ""
    return str(paths[0]), f"{key}#{_pick_variant(key, store.today_local(), variant) + 1}"
