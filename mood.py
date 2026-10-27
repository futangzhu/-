"""情绪：由"今天发生了什么"推出来，不是模型随口给的形容词。

为什么不交给模型：心情应当有来处。她说"今天有点闷"，理由必须是"没人接我话"或
"有人跟我拌了两句"，而不是模型随机抽到一个词。规则算出来的缺点是不够细腻，
但优点是可解释、可复现、不会今天晴明天雨。

输入只有三样：今天的事件情感分布、主动开口有没有人接、说过几句话。
"""

from __future__ import annotations

import datetime

from . import store

# (下限, 说法)。说话用短词，别用"情绪状态良好"这种词
LABELS: list[tuple[float, str]] = [
    (0.85, "心情很好"),
    (0.65, "还不错"),
    (0.45, "平平的"),
    (0.28, "有点闷"),
    (0.0, "不太高兴"),
]


def score(now: datetime.datetime | None = None) -> dict:
    """返回 {score, label, reasons}。分数 0–1，基准 0.55。"""
    now = now or datetime.datetime.now(store.TZ)
    today = store.today_local()
    reasons: list[str] = []
    s = 0.55

    events = store.events_on_day(today)
    pos = sum(1 for e in events if e.get("sentiment") == "正")
    neg = sum(1 for e in events if e.get("sentiment") == "负")
    if pos:
        s += min(0.3, 0.12 * pos)
        reasons.append(f"今天有 {pos} 件好事")
    if neg:
        s -= min(0.35, 0.12 * neg)
        reasons.append(f"有 {neg} 件不顺的")

    pro = store.proactive_on_day(today)
    replied = sum(1 for r in pro if r.get("replied"))
    ignored = sum(1 for r in pro if not r.get("replied"))
    if replied:
        s += min(0.2, 0.1 * replied)
        reasons.append(f"她自己开口 {replied} 次，有人接")
    if ignored:
        s -= min(0.3, 0.15 * ignored)
        reasons.append(f"{ignored} 次没人理")

    n_speech = store.speech_count_on_day(today)
    if n_speech >= 20:
        s += 0.05
        reasons.append("今天话不少")

    s = max(0.0, min(1.0, round(s, 2)))
    label = next(text for floor, text in LABELS if s >= floor)
    if not reasons:
        reasons.append("今天没什么事")
    return {"score": s, "label": label, "reasons": reasons}


def brief(now: datetime.datetime | None = None, max_chars: int = 24) -> str:
    """给状态块用的一句话，尽量短。"""
    r = score(now)
    return f"这会儿{r['label']}。"[:max_chars]
