"""主动开口：她决定说点什么。

闸门负责"能不能说"，这里负责"说什么"。分开的好处是：措辞可以在离线环境里反复测，
不用真的往群里发消息；而闸门是纯逻辑，也能单独测。

三条约束（写在这里，也写进提示词）：
- 短：一句话，最多 25 字。主动搭话还长篇大论，跟客服无差别。
- 不提问：不"在吗""有人吗""大家今天过得怎么样"。主动不等于发起调查。
- 有由头：从"她此刻在干什么"或"她记得的某件事"里挑一个，不凭空找话。
"""

from __future__ import annotations

import datetime
import re

from . import recall, relation, state, store

MAX_CHARS = 40
SYSTEM = """你在替一个角色说一句话。她叫大肥鱼，是住在机器里的 AI 鲸鱼娘，把 token 叫"白饭"。
傲娇，懒，怕麻烦，嘴上不认，心里认。

现在她**没有被叫**，是自己想在群里开口。请写出她要说的那一句。

规则：
1. 只输出这句话本身。不要引号、不要解释、不要写"她说："。
2. 一到两小句，总共不超过 25 字。可以带括号里的小动作，比如（敲了敲盆）。
3. 不许提问式开场：不要"在吗""有人吗""大家在干嘛""今天过得怎么样"。
4. 必须有一个由头——接下来给你的【此刻】和【她记得的事】里挑一个，不要凭空找话。
5. 不许自我介绍，不许提"我是 AI""我是机器人"。
6. 说得像自言自语也行，冷场了也没关系。
7. 如果给了"她最近说过"，不要重复那些开头和括号里的动作——她已经连着用同一个姿势了。
8. 括号里的动作最多每三句用一次。连着两句都以"（"开头，就是套路。
9. 不要每句都回到"白饭"。它是她的口头禅，不是她唯一的话题。
10. 【此刻】里若出现"被人问…""如果有人…"这种写法，那是条件和待办，不是已经发生的事。
    不要替一个没人问的问题写答案，也不要用"还行""就那样""没什么"这类应答开头。
11. 没人说话也可以说一句自己的事。冷场不是错，硬接话才是。"""


def clean(text: str) -> str:
    """把模型输出收拾成可以直接发的一句话。"""
    t = (text or "").strip()
    t = re.sub(r"^(她说|大肥鱼说|回复)[：:]\s*", "", t)
    t = t.strip().strip("“”\"'「」『』")
    t = re.sub(r"\s*\n+\s*", " ", t).strip()
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS].rstrip("，,、；;")
    return t


def looks_like_customer_service(text: str) -> bool:
    """客服腔检测。出现就丢掉重来——这类话比她不说更伤。"""
    bad = ("在吗", "有人吗", "大家在干嘛", "今天过得怎么样", "有什么可以帮",
           "需要我", "随时找我", "欢迎", "请问")
    return any(b in text for b in bad)


_ACTION_RE = re.compile(r"^\s*[（(][^）)]*[）)]\s*")
_ANSWER_HEADS = ("周末", "还行", "就那样", "没什么", "挺好的", "跟平时一样", "也还行",
                 "还不赖", "一般般", "凑合", "差不多吧")


def _strip_actions(text: str) -> str:
    """去掉开头的括号动作，好判断真正的第一句是什么。"""
    t = text or ""
    while True:
        n = _ACTION_RE.sub("", t)
        if n == t:
            return t
        t = n


def looks_like_answer(text: str) -> bool:
    """像在回答一个没人问的问题。

    主动开口时出现这种开头，多半是把计划里的条件剧本当成了事实——
    2026-10-26 18:16 那句"周末啊……混过去就行了吧"就是这么来的。
    """
    t = _strip_actions(text).lstrip("，,、 ")
    return t.startswith(_ANSWER_HEADS)


def _quiet_minutes(session: str, now: datetime.datetime) -> int | None:
    """群里上一次有人说话是几分钟前。没记录返回 None。"""
    p = store.last_incoming(session)
    if not p:
        return None
    try:
        return int((now - store.to_local(p["ts"])).total_seconds() // 60)
    except Exception:  # noqa: BLE001
        return None


def build_prompt(session: str, now: datetime.datetime | None = None) -> str:
    now = now or datetime.datetime.now(store.TZ)
    # mode="proactive"：只给"她在干什么"，不给"如果有人问就怎么答"
    parts = [state.build(session, now, mode="proactive")]

    p = store.last_incoming(session)
    if p:
        mins = int((now - store.to_local(p["ts"])).total_seconds() // 60)
        parts.append(f"群里上一次有人说话是 {mins} 分钟前。")

    # 记忆只取最近的几条，别把主动搭话变成念档案
    events = store.active_events_by_session(session, limit=12)
    if events:
        lines = "；".join(e["summary"] for e in events[-3:])
        parts.append(f"她记得的事：{lines}")

    recent = store.recent_speech_texts(session, 3)
    if recent:
        joined = " / ".join(x[:20] for x in recent)
        parts.append(f"她最近说过：{joined}")
        parts.append("不要再用同样的开头或同样的括号动作，换一个。")

    person = store.get_person(session)
    if person and int(person.get("relation") or 0):
        parts.append(f"你跟他：{relation.describe(int(person['relation']))}。")

    parts.append("现在写她要说的那一句。")
    return "\n".join(p for p in parts if p)


def starts_with_action(text: str) -> bool:
    """是不是以括号动作开头。这类句式连用两次就会显形。"""
    return text.lstrip().startswith(("（", "("))


async def compose(provider, session: str, now: datetime.datetime | None = None) -> str:
    """生成一句主动发言。失败或像客服，返回空字符串。

    上一句若以括号动作开头，这一句先要求换个写法；模型不听就再要一次，
    第二次仍如此就放过——宁可重复一次句式，也不要不说话。
    """
    if provider is None:
        return ""
    now = now or datetime.datetime.now(store.TZ)
    quiet = _quiet_minutes(session, now)
    recent = store.recent_speech_texts(session, 1)
    avoid_action = bool(recent and starts_with_action(recent[0]))
    prompt = build_prompt(session, now)

    for attempt in range(2):
        ask = prompt if not (avoid_action and attempt == 0) else (
            prompt + "\n这一句不要用括号里的动作开头，直接说话。")
        resp = await provider.text_chat(prompt=ask, system_prompt=SYSTEM)
        text = clean(getattr(resp, "completion_text", "") or "")
        if not text or looks_like_customer_service(text):
            continue
        # 群里半天没人说话，却冒出一句应答腔 —— 那就是在替没人问的问题作答
        if quiet is None or quiet >= 20:
            if looks_like_answer(text):
                continue
        if avoid_action and attempt == 0 and starts_with_action(text):
            continue
        return text
    return ""


def remember_spoken(session: str, text: str, willingness: float) -> int:
    """记下她主动说过的话，给冷却和退避用。"""
    pid = store.record_proactive(session, willingness, text=text)
    store.record_speech(session, len(text), kind="proactive", text=text)
    return pid
