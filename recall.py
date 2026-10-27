"""检索与渲染 —— 目标不是"查得到"，是"像想起来"。

数据模型.md §4 的三条硬规矩：
1. 不写"记录显示""之前提到过"
2. 只用模糊时间：前几天 / 上次 / 前阵子，不出现精确日期
3. 允许不完整、允许记混一点 —— 那反而像人

调参记录：
- 00:20 关键词权重 3 → 6，改子串匹配（"猫"这种单字关键词原本永远命中不了）
- 00:22 时间词改为最终排序后再分配，否则会出现"还有，……"当开头
"""

from __future__ import annotations

import datetime
import json
import re

from . import store

BUDGET_CHARS = 200
CJK = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}")
FILLERS = ("还有，", "另外，")


def _tokens(text: str) -> set[str]:
    """粗切关键词：中文取 2 字滑窗，英文数字取整词。够用，且不依赖分词库。"""
    out: set[str] = set()
    for seg in CJK.findall(text or ""):
        if seg.isascii():
            out.add(seg.lower())
        else:
            out.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def _time_word(delta_days: int) -> str:
    if delta_days <= 0:
        return "今天"
    if delta_days == 1:
        return "昨天"
    if delta_days <= 3:
        return "这两天"
    if delta_days <= 10:
        return "前几天"
    if delta_days <= 40:
        return "上个月"
    return "前阵子"


USED_PER_DAY = 7          # 最近七天提过的事，再提就降权
USED_PENALTY = 3.0        # 提过一次降 3 分，提得越多越不想再说

def _score(ev: dict, scene_kw: set[str], scene_text: str, now: datetime.datetime,
           used: set[int] | None = None) -> float:
    score = float(ev.get("importance") or 1) * 2.0

    local_dt = store.to_local(ev.get("ts", ""))
    days = max(0, (now.date() - local_dt.date()).days)
    score += max(0.0, 9.0 - days) / 3.0          # 越新越容易想起来，但不压倒重要度

    if used and ev.get("id") in used:
        score -= USED_PENALTY          # 同一件事已经说过了，别絮叨

    kws = [k for k in (ev.get("keywords") or "").split() if k]
    if kws and scene_text:
        # 子串命中：单个"猫"也能匹配"那只猫还在吗"
        hits = sum(1 for kw in kws if kw in scene_text or kw.lower() in scene_kw)
        score += hits * 6.0
    return score


def _days_ago(ev: dict, now: datetime.datetime) -> int:
    return max(0, (now.date() - store.to_local(ev.get("ts", "")).date()).days)


def retrieve(uid: str, scene_text: str = "", k: int = 5,
             budget: int = BUDGET_CHARS) -> dict:
    """取"印象"。返回 {"text": 渲染结果, "ids": [...], "items": [...]}。"""
    now = datetime.datetime.now(store.TZ)
    scene_kw = _tokens(scene_text)
    events = store.active_events(uid, limit=50)
    if not events:
        return {"text": "", "ids": [], "items": []}

    used = store.recent_used_event_ids(uid, days=USED_PER_DAY)
    ranked = sorted(events, key=lambda e: _score(e, scene_kw, scene_text, now, used), reverse=True)

    # 先挑，后渲染：挑的时候只看摘要长度，避免"还有"占位影响预算
    picked: list[dict] = []
    used = 0
    for ev in ranked:
        if len(picked) >= k:
            break
        summary = (ev.get("summary") or "").strip().rstrip("。") or (ev.get("keywords") or "").strip()
        if not summary:
            continue
        if used + len(summary) > budget and picked:
            break
        picked.append(ev)
        used += len(summary)

    picked.sort(key=lambda e: e.get("ts") or "")   # 按时间正序读

    seen_time: set[str] = set()
    filler_i = 0
    pieces: list[str] = []
    for ev in picked:
        tw = _time_word(_days_ago(ev, now))
        if tw not in seen_time:
            seen_time.add(tw)
            prefix = tw
        else:
            prefix = FILLERS[filler_i % len(FILLERS)]
            filler_i += 1
        summary = (ev.get("summary") or "").strip().rstrip("。")
        pieces.append(f"{prefix}{summary}")

    # 周记：久远的事以粗颗粒出现。只在预算还够、且跟当下有点关系时才带
    roll_line, roll_period = _pick_rollup(uid, scene_text, budget - used)
    if roll_line:
        pieces.insert(0, roll_line)

    text = "；".join(p for p in pieces if p)
    if text:
        text += "。"
    return {
        "rollup": roll_period,
        "text": text,
        "ids": [e["id"] for e in picked],
        "items": [{**e, "_text": p} for e, p in zip(picked, pieces)],
    }


def _pick_rollup(uid: str, scene_text: str, budget_left: int) -> tuple[str, str]:
    """挑一条周记当背景。宁可不带，也不硬塞——久远的轮廓只在有用时才提。"""
    if budget_left < 20:
        return "", ""
    for r in store.rollups_for(uid, limit=3):
        summary = (r.get("summary") or "").strip().rstrip("。")
        if not summary:
            continue
        line = "那阵子" + summary
        if len(line) > budget_left:
            continue
        if scene_text:
            hit = any(tok and tok in scene_text for tok in _tokens(summary))
            if not hit:
                continue
        return line, str(r.get("period") or "")
    return "", ""


def person_line(uid: str, max_chars: int = 60) -> str:
    """人物卡：称呼 + 一句印象，预算 60 字（数据模型.md §5）。"""
    p = store.get_person(uid)
    if not p:
        return ""
    name = ""
    try:
        names = json.loads(p.get("names") or "{}")
        name = next(iter(names.values()), "") if names else ""
    except (ValueError, TypeError):
        name = ""
    parts = [x for x in (name, (p.get("impression") or "").strip()) if x]
    return "，".join(parts)[:max_chars]
