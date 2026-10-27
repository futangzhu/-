"""主动意愿：从"她在干什么"推出"她此刻会不会开口"。

这一步只算分，不发消息——真正的发送要走 M6 的闸门（冷却、被无视退避、群里有没有人）。

为什么值得单独一层：日程里写了"群里冒个泡"，那这句话本身就是一个意图。
把意图丢掉，只留语气，日程就白写了；把意图直接变成发送，又会变成"到点就冒泡"的机器人。
所以中间放一层意愿分：日程给意愿，闸门给许可，模型给措辞。
"""

from __future__ import annotations

import datetime

from . import state, store

# 权重都写在这里，方便按观察结果调。分值不追求精确，只要求"排序对"
W_ATTENTION = {"idle": 0.35, "aware": 0.25, "busy": 0.0}
W_CHATTY = 0.30          # 活动本身就跟人有关（群里、聊天、翻旧账）
W_IDLE_ACT = 0.15        # 冒泡、发呆这类"没有明确目标"的动作
W_ENERGY = 0.20          # 能量满格时更容易开口
THRESHOLD = 0.55         # 超过此分才算"有可能会说"

CHATTY_WORDS = ("群里", "聊天", "翻旧账", "拌嘴", "看人", "围观")
IDLE_WORDS = ("冒泡", "发呆", "闲着", "晃", "坐着", "趴着")


def score(now: datetime.datetime | None = None) -> dict:
    """返回 {score, verdict, reasons, block}。睡着时为 0。"""
    now = now or datetime.datetime.now(store.TZ)
    if state.is_asleep(now):
        return {"score": 0.0, "verdict": "睡着，不主动", "reasons": ["在睡眠时段"], "block": None}

    blk = store.current_block(store.today_local(), now.strftime("%H:%M"))
    if not blk:
        return {"score": 0.0, "verdict": "没有活动块，不主动", "reasons": ["今天没有计划"], "block": None}

    act = (blk.get("activity") or "") + " " + (blk.get("place") or "")
    s = 0.0
    reasons = []

    att = blk.get("attention") or "aware"
    s += W_ATTENTION.get(att, 0.2)
    reasons.append(f"attention={att} +{W_ATTENTION.get(att, 0.2):.2f}")

    if any(w in act for w in CHATTY_WORDS):
        s += W_CHATTY
        reasons.append(f"活动跟人有关 +{W_CHATTY:.2f}")
    if any(w in act for w in IDLE_WORDS):
        s += W_IDLE_ACT
        reasons.append(f"手上没明确目标 +{W_IDLE_ACT:.2f}")

    energy = int(blk.get("energy") or 50)
    s += energy / 100 * W_ENERGY
    reasons.append(f"energy={energy} +{energy / 100 * W_ENERGY:.2f}")

    s = round(min(1.0, s), 2)
    verdict = "有可能会开口" if s >= THRESHOLD else "大概率不开口"
    return {"score": s, "verdict": verdict, "reasons": reasons, "block": blk}


def explain(now: datetime.datetime | None = None) -> str:
    r = score(now)
    lines = [f"主动意愿 {r['score']:.2f} / 阈值 {THRESHOLD} → {r['verdict']}"]
    blk = r["block"]
    if blk:
        lines.append(f"当前块 {blk['start_at']}–{blk['end_at']}：{blk['activity']}（{blk['place']}）")
    lines.append("依据：" + "；".join(r["reasons"]))
    return "\n".join(lines)
