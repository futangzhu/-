"""闸门：此刻这个群，能不能让她自己开口。

分三层是刻意的：日程给"意愿"，闸门给"许可"，模型给"措辞"。
把日程直接接到发送上，第二天就会变成"到点冒泡"的闹钟——跟"到点必回"是同一个毛病。

默认关闭。要她真的主动说话，得在插件配置里把 proactive_enabled 打开。
"""

from __future__ import annotations

import datetime

from . import proactivity, relation, state, store

COOLDOWN_MIN = 40        # 同一个群，两次主动至少隔这么久
ALIVE_MIN = 30           # 群里最近这么久有人说话，才算"活着"
RETREAT_DAYS = 7         # 统计窗口：只看最近 7 天
RETREAT_IGNORES = 2

# 私聊的参数跟群里完全不同：私聊是"她来找人"，不是"她插一句话"
PRIVATE_COOLDOWN_HOURS = 20      # 对同一个人，一天最多找一次
PRIVATE_MIN_RELATION = relation.SPEAK_THRESHOLD   # 好感度门槛：到"比较喜欢"才找
PRIVATE_UNANSWERED_LIMIT = 1     # 上一条还没人接，就不追第二句


def can_speak(session: str, enabled: bool = False,
              now: datetime.datetime | None = None) -> dict:
    """返回 {ok, reason, score}。reason 是给人看的一句话。"""
    now = now or datetime.datetime.now(store.TZ)

    if not enabled:
        return {"ok": False, "reason": "未启用（proactive_enabled=false）", "score": 0.0}

    if state.is_asleep(now):
        return {"ok": False, "reason": "在睡眠时段，任何人都不吵", "score": 0.0}

    p = proactivity.score(now)
    if p["score"] < proactivity.THRESHOLD:
        return {"ok": False, "reason": f"意愿 {p['score']:.2f} 没过线", "score": p["score"]}

    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=RETREAT_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    streak = store.unanswered_streak(session, since)
    if streak >= RETREAT_IGNORES:
        return {"ok": False,
                "reason": f"最近 {streak} 次开口都没人接，先不插嘴了（有人说话就恢复）",
                "score": p["score"]}

    last = store.last_proactive(session)
    if last:
        mins = int((now - store.to_local(last["ts"])).total_seconds() // 60)
        if mins < COOLDOWN_MIN:
            return {"ok": False,
                    "reason": f"冷却中，距上次主动 {mins} 分钟（下限 {COOLDOWN_MIN}）",
                    "score": p["score"]}

    incoming = store.last_incoming(session)
    if not incoming:
        return {"ok": False, "reason": "这个群没有任何来话记录，不主动暖死群", "score": p["score"]}
    mins = int((now - store.to_local(incoming["ts"])).total_seconds() // 60)
    if mins > ALIVE_MIN:
        return {"ok": False,
                "reason": f"群里 {mins} 分钟没人说话了，不主动暖死群",
                "score": p["score"]}

    return {"ok": True, "reason": f"可以开口（意愿 {p['score']:.2f}）", "score": p["score"]}


def explain(session: str, enabled: bool = False) -> str:
    r = can_speak(session, enabled)
    lines = [f"闸门：{'放行' if r['ok'] else '拦住'} —— {r['reason']}"]
    p = proactivity.score()
    blk = p["block"]
    if blk:
        lines.append(f"当前块 {blk['start_at']}–{blk['end_at']}：{blk['activity']}（{blk['place']}）")
    lines.append(f"规则：睡眠不发｜同群冷却 {COOLDOWN_MIN} 分钟｜"
                 f"{ALIVE_MIN} 分钟内有人说话才算活群｜连续 {RETREAT_IGNORES} 次没人接就先不插嘴（有人说话自动恢复）")
    return "\n".join(lines)


def can_speak_private(uid: str, enabled: bool = False, relation: int = 0,
                      now: datetime.datetime | None = None) -> dict:
    """私聊要不要主动找他。

    跟群里的区别：这里没有"群还活着吗"这种判断，只有"我跟他熟不熟""他接不接我话"。
    退避不靠时钟、不设封禁期——她不是被罚禁言，只是不追第二条。
    他哪天回了一句，这个状态自然就解开了。
    """
    now = now or datetime.datetime.now(store.TZ)

    if not enabled:
        return {"ok": False, "reason": "私聊主动未启用", "score": 0.0}
    if state.is_asleep(now):
        return {"ok": False, "reason": "在睡眠时段", "score": 0.0}
    if relation < PRIVATE_MIN_RELATION:
        return {"ok": False, "reason": f"好感度 {relation} 不到 {PRIVATE_MIN_RELATION}", "score": 0.0}

    last = store.last_proactive(uid)
    if last:
        hours = (now - store.to_local(last["ts"])).total_seconds() / 3600
        if not last["replied"]:
            return {"ok": False, "reason": "上一条他还没回，不追第二句", "score": 0.0}
        if hours < PRIVATE_COOLDOWN_HOURS:
            return {"ok": False, "reason": f"今天已经找过他了（{hours:.0f} 小时前）", "score": 0.0}

    p = proactivity.score(now)
    return {"ok": True, "reason": "可以找他", "score": p["score"]}
