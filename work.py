"""工作量记录：她知道自己/分身动过什么活。

不入侵任何别的插件——这里记的是"发生过什么"，不是"怎么发生的"。分三类触发源：

    manual      主人一条指令让她做的（/补一天、/说说 写、/抽一次）
    scheduled   按日程该做的（每天补日记、写说说稿）
    passive     顺手做的（有人说话时抽记忆）

以及两种执行者（actor）：

    clone   分身——所有"服务性"的活。分身是工具，没有情绪，也没有脾气
    self    她本体——写日记、发呆、说话。日记绝不能挂到分身头上

口径（主人 2026-10-26 02:55–02:59 定）：活是分身干的，她偷懒；但分身是她的延伸，
出了错她认。分身不需要情感，免得以后出问题。

`task` 写人话，不写参数、不写库名、不写代码痕迹。
"""

from __future__ import annotations

import datetime

from . import store

TRIGGERS = ("manual", "scheduled", "passive")

# 分身的三种说法。它没有情绪，所以这里也不给它情绪
CLONE_PHRASE = {
    "manual": "分身被你叫去干活了",
    "scheduled": "分身把该做的做了",
    "passive": "分身顺手记了一笔",
}


def log(task: str, trigger: str = "manual", source: str = "self",
        status: str = "ok", note: str = "", actor: str = "clone") -> int:
    trigger = trigger if trigger in TRIGGERS else "manual"
    actor = actor if actor in ("clone", "self") else "clone"
    return store.add_work(task, trigger, source, status, note, actor)


def recent(minutes: int = 10) -> list[dict]:
    return store.work_since(minutes)


def brief(minutes: int = 10, max_chars: int = 40) -> str:
    """状态块里那一行。没活就返回空字符串，绝不硬凑。"""
    rows = recent(minutes)
    if not rows:
        return ""
    last = rows[-1]
    task = (last.get("task") or "").strip()
    if not task:
        return ""

    if last.get("actor") == "self":
        line = f"你刚才{task}。"
    else:
        phrase = CLONE_PHRASE.get(last.get("trigger"), "分身干了点活")
        if last.get("status") == "fail":
            phrase = "分身干活没成"
        line = f"刚才{phrase}：{task}。"
    return line[:max_chars]


def _time_word(ts_utc: str) -> str:
    """模糊时间。她不会说"10 月 25 日 14 时"，只会说"昨天"。"""
    try:
        day = store.to_local(ts_utc).date()
    except Exception:  # noqa: BLE001
        return ""
    delta = (datetime.datetime.now(store.TZ).date() - day).days
    if delta <= 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta <= 3:
        return "这两天"
    return "前几天"


def summary(days: int = 7, max_items: int = 5, max_chars: int = 120) -> str:
    """被问到"谁弄的""前几天帮我弄过吗"时的依据。

    只报实际发生过的事，带模糊时间，不编、不补、不排比。
    """
    rows = store.work_recent_days(days, limit=60)
    if not rows:
        return ""

    seen: set[str] = set()
    clone, mine = [], []
    for r in rows:                      # 已按时间倒序，先出现的更新
        task = (r.get("task") or "").strip()
        if not task or task in seen:
            continue
        seen.add(task)
        item = f"{_time_word(r.get('ts') or '')}{task}"
        (mine if r.get("actor") == "self" else clone).append(item)

    parts = []
    if clone:
        parts.append("分身：" + "、".join(clone[:max_items]))
    if mine:
        parts.append("你自己：" + "、".join(mine[:3]))
    return "；".join(parts)[:max_chars]


# 问起"这活谁干的"时能触发上面那段的话
WORK_QUESTION_WORDS = ("分身", "谁弄", "谁做的", "谁帮", "插件", "干活",
                       "日记", "说说", "记忆库", "生日")


def is_work_question(text: str) -> bool:
    t = (text or "").strip()
    return any(w in t for w in WORK_QUESTION_WORDS)
