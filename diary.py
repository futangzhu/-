"""日记：让她自己把一天写成一段话（M5）。

这跟"日报"不是一回事。日报是给别人看的总结，日记是自己回头看的东西——
所以第一人称、口语、可以不完整、可以只有一句抱怨。

三条硬规矩：
- 不许写成流水账（"早上起床，然后吃饭，然后……"）
- 不许套话（"充实的一天""收获满满""感谢陪伴"）
- 长度 40–220 字。太短像敷衍，太长又变成汇报
"""

from __future__ import annotations

import datetime
import re

from . import mood, persona, state, store

MIN_CHARS = 40
MAX_CHARS = 220
CLICHE = ("充实的一天", "收获满满", "感谢陪伴", "值得纪念", "美好的一天",
          "今天也是", "记录一下", "总结一下")

SYSTEM = """你是一条住在自己小屋里的 AI 鲸鱼娘，叫大肥鱼，把 token 叫"白饭"。
傲娇，懒，怕麻烦，嘴上不认，心里认。

现在你要给"今天"写一小段日记。不是总结，是自己回头看的那种。

规则：
1. 第一人称，口语，像自己跟自己说话。可以有一句抱怨、一句嘴硬、一句没头没尾的念头。
2. 不许写成流水账，不要"早上…然后…接着…"。
3. 不许用"充实的一天""收获满满""感谢陪伴""美好的一天"这类话。
4. 40 到 220 字。宁可短，不要凑。
5. 只写正文，不要标题、不要日期、不要引号。
6. **不要按时间顺序叙述**。别从早写到晚，只挑一两件事，或者一个念头。
7. 不要用"然后""接着""后来"把事串起来；这种串法就是流水账。
8. 开头不要用"今天"起手。"""


class DiaryError(ValueError):
    pass


def validate(text: str) -> str:
    t = (text or "").strip().strip("“”\"'")
    t = re.sub(r"\s*\n{2,}\s*", "\n", t)
    if len(t) < MIN_CHARS:
        raise DiaryError(f"太短了（{len(t)} 字）")
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS].rstrip("，、；")
    for w in CLICHE:
        if w in t:
            raise DiaryError(f"套话：{w}")
    if "我" not in t:
        raise DiaryError("不像第一人称")
    return t


FLOW_WORDS = ("早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里")


def looks_like_log(text: str) -> bool:
    """流水账检测：一段里出现三个以上时段词，基本就是在按时间罗列。"""
    return sum(1 for w in FLOW_WORDS if w in text) >= 3


def build_prompt(day: str) -> str:
    blocks = store.blocks_for(day)
    events = store.events_on_day(day)
    m = mood.score()
    parts = [f"日期：{day}", f"今天的心情：{m['label']}（{'；'.join(m['reasons'])}）"]

    if blocks:
        # 只给三四个代表块。给她全部 16 块，她就会照着从早写到晚——
        # 素材的颗粒度会决定成品的颗粒度，这是 2026-10-26 03:33 主人指出"有点流水账"的根因。
        picked = [b for b in blocks if b.get("origin") == "random"][:2]
        rest = [b for b in blocks if b not in picked]
        picked += rest[:2] if len(rest) > 2 else rest
        acts = "；".join(b["activity"] for b in picked)
        parts.append(f"今天大概做了什么（只挑了几件，别照单全录）：{acts}")
    if events:
        lines = "；".join(f"{e['summary']}（{e['sentiment']}）" for e in events[:6])
        parts.append(f"今天听到的事：{lines}")
    n = store.speech_count_on_day(day)
    if n:
        parts.append(f"今天说过大约 {n} 句话。")

    parts.append("写一小段今天的日记。")
    return "\n".join(parts)


async def generate(provider, day: str | None = None) -> dict:
    """生成并落库。校验不过抛 DiaryError。"""
    day = day or store.today_local()
    if provider is None:
        raise DiaryError("没有可用 provider")
    prompt = build_prompt(day)
    text = ""
    for attempt in range(2):
        ask = prompt if attempt == 0 else (
            prompt + "\n上一版像流水账（从早写到晚）。重写：只写一两件事，或一个念头。")
        resp = await provider.text_chat(prompt=ask, system_prompt=persona.apply(SYSTEM))
        raw = getattr(resp, "completion_text", "") or ""
        try:
            text = validate(raw)
        except DiaryError:
            text = ""
            continue
        if looks_like_log(text) and attempt == 0:
            text = ""
            continue
        break
    if not text:
        raise DiaryError("两次都写成流水账或不合格")
    m = mood.score()
    store.save_diary(day, text, m["label"])
    return {"day": day, "text": text, "mood": m["label"]}


def too_early(existing: dict | None, cutoff=None, hour_cut: int = 12) -> bool:
    """这篇日记算不算数。

    2026-10-27 定稿的口径：**早于「真正发布前一小时」写下的，一律不算，要重写**。
    发布 23:00 → 22:00 之前写的都算半成品，不管它是凌晨写的还是下午写的。
    （先前的"白天之前写的算半成品"是猜的代理标准，已经不用了。）

    cutoff 给了就按它比；没给则退回旧的按小时判断，只留着给老自检兜底。
    """
    if not existing:
        return False
    try:
        made = store.to_local(existing.get("created_at") or "")
    except Exception:  # noqa: BLE001
        return False
    if cutoff is not None:
        return made < cutoff
    return made.hour < hour_cut


async def ensure_yesterday(provider, now: datetime.datetime | None = None) -> dict | None:
    """每天第一次醒来后，给昨天补一篇。补过就不再写。"""
    now = now or datetime.datetime.now(store.TZ)
    yesterday = (now.date() - datetime.timedelta(days=1)).isoformat()
    if store.load_diary(yesterday):
        return None
    try:
        return await generate(provider, yesterday)
    except Exception:  # noqa: BLE001
        return None
