"""周记压缩：不是省空间，是给记忆分出层次。

主人 2026-10-26 说过"我们现在内存很充足"。所以这里的压缩**不删任何东西**：
原文一条不动，只是额外生成一层粗粒度的摘要，让检索有远近之分。

为什么需要：事件攒到几百条后，每条都是"前几天他说…"，检索出来的东西会变得同质，
她能想起来的永远是最近那几条。有了周记，久远的事还能以"那阵子他在搬家"这种
粗颗粒被想起来——人记事情本来就是这样的：远的记得轮廓，近的记得细节。

为什么不用模型：摘要必须只由已有摘要拼成，一个字的推断都不加。调模型会润色，
润色就会长出原话里没有的东西，那是记忆污染。

层次：
    level='week'    一周一条，粗粒度，长期保留
    level='month'   一月一条，更粗（暂未启用）
    event           原文，按重要度衰减，永不删除
"""

from __future__ import annotations

import datetime

from . import store

MAX_ITEMS = 5           # 一周最多并进 5 件事，多了就成流水账
MIN_IMPORTANCE = 3      # 只压"有信息量"的，闲聊不进来


def week_key(day: datetime.date) -> str:
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def week_bounds(day: datetime.date) -> tuple[str, str]:
    monday = day - datetime.timedelta(days=day.weekday())
    return monday.isoformat(), (monday + datetime.timedelta(days=6)).isoformat()


def build_week(uid: str, day: datetime.date) -> dict | None:
    """把某一周的事件压成一句粗颗粒的摘要。纯拼接，不推断。"""
    start, end = week_bounds(day)
    events = store.events_between(uid, start, end)
    picked = [e for e in events if int(e.get("importance") or 1) >= MIN_IMPORTANCE]
    if not picked:
        return None
    picked.sort(key=lambda e: (int(e.get("importance") or 1), e.get("ts") or ""), reverse=True)
    picked = picked[:MAX_ITEMS]

    parts = []
    for e in picked:
        s = (e.get("summary") or "").strip().rstrip("。")
        if s and s not in parts:
            parts.append(s)
    if not parts:
        return None

    summary = "；".join(parts)
    return {"uid": uid, "level": "week", "period": week_key(day), "summary": summary}


def ensure_last_week(day: datetime.date | None = None) -> list[dict]:
    """给上周写周记。写过就跳过（表上有 UNIQUE）。"""
    day = day or datetime.datetime.now(store.TZ).date()
    last_week_day = day - datetime.timedelta(days=7)
    made = []
    for uid in store.uids_with_events():
        row = build_week(uid, last_week_day)
        if not row:
            continue
        if store.load_rollup(uid, "week", row["period"]):
            continue
        store.save_rollup(uid, "week", row["period"], row["summary"])
        made.append(row)
    return made
