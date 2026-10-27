"""从对话里抽取"值得记住的事"（M4）。

原则（数据模型.md §3、设计.md 第 5 节）：
- 抽的是"人"和"事"，不是"聊天记录"。原话不进库。
- 摘要用第三人称一句话，像"她记得的"而不是"日志里写的"。
- 重要度决定衰减：1 级 7 天、2 级 30 天、3 级 90 天、4-5 级永久。
- 宁可漏，不要错。不确定的不记，记错的比不记更伤——她会拿错的事去接话。

调用失败、格式不对、校验不过，一律丢弃并记日志，不写库。
"""

from __future__ import annotations

import json
import re

from . import store

MAX_EVENTS = 3
MAX_SUMMARY = 40
KINDS = ("info", "promise", "conflict", "mood", "first")
SENTIMENTS = ("正", "中", "负")

# 开发/工具对话：不该进她的记忆库。私聊里也会聊这些，挡词比挡会话准。
DEV_WORDS = ("提示词", "插件", "重启", "重载", "报错", "日志", "接口", "部署",
             "自检", "selftest", "代码", "仓库", "commit", "bug", "调试", "配置",
             "数据库", "schema", "api", "token 数", "上下文长度")

# 不值得记的话：寒暄、表情、指令、太短的
SKIP_PATTERNS = (
    r"^[\s\?？!！。.…~～]*$",
    r"^[哈h]{2,}$",
    r"^(在吗|在不在|早|晚安|你好|hi|hello)[\s!！。.]*$",
)

SYSTEM = """你在帮一个角色做记忆整理。她叫大肥鱼，是住在机器里的 AI 鲸鱼娘，把 token 叫"白饭"。

给你一段刚发生的对话（用户说的、她回的），你要判断：这里面有什么是"值得她以后记住"的。

只输出 JSON：
{"person": {"name": "", "impression": "一句对她的印象，不超过 20 字，没有就空"},
 "events": [{"summary": "第三人称一句话，不超过 30 字", "kind": "info|promise|conflict|mood|first", "keywords": "空格分开的 2-5 个词", "sentiment": "正|中|负", "importance": 1-5}]}

规则：
1. 只记"关于用户的事实、心情、约定、冲突、第一次"。不记寒暄、不记她自己说过的话、不记聊天记录本身。
2. summary 像"他说周末要搬家，房东临时变卦"，不要写"用户表示""对话中提到"。
3. importance：1 闲聊／2 一般／3 有具体信息／4 约定或情绪明显／5 重要到不该忘。
4. 没有值得记的就输出 {"person": {}, "events": []}。宁可空，不要编。
5. 最多 3 条 events。

只输出 JSON。"""


class ExtractError(ValueError):
    pass


def should_skip(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 6:
        return True
    if t.startswith("/") or t.startswith("／"):
        return True
    for pat in SKIP_PATTERNS:
        if re.match(pat, t, re.IGNORECASE):
            return True
    low = t.lower()
    if any(w.lower() in low for w in DEV_WORDS):
        return True
    return False


def extract_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ExtractError("输出里没有 JSON")
    try:
        return json.loads(raw[start:end + 1])
    except ValueError as e:
        raise ExtractError(f"JSON 解析失败：{e}") from e


def validate(data: dict) -> tuple[dict, list[dict]]:
    """返回 (person, events)。不合规的条目直接丢，不让它污染库。"""
    if not isinstance(data, dict):
        raise ExtractError("顶层不是对象")

    person = data.get("person") if isinstance(data.get("person"), dict) else {}
    p = {
        "name": str(person.get("name") or "").strip()[:20],
        "impression": str(person.get("impression") or "").strip()[:40],
    }

    events: list[dict] = []
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raw_events = []

    for item in raw_events[:MAX_EVENTS]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip().rstrip("。")
        if not summary or len(summary) > MAX_SUMMARY:
            continue
        if re.search(r"用户表示|对话中|记录显示|提到过", summary):
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "info"
        sentiment = item.get("sentiment") if item.get("sentiment") in SENTIMENTS else "中"
        try:
            imp = int(item.get("importance") or 1)
        except (TypeError, ValueError):
            imp = 1
        imp = max(1, min(5, imp))
        kws = " ".join(str(item.get("keywords") or "").split()[:5])
        events.append({"summary": summary, "kind": kind, "sentiment": sentiment,
                       "importance": imp, "keywords": kws})

    return p, events


async def extract(provider, user_text: str, bot_text: str = "") -> tuple[dict, list[dict]]:
    """调模型抽取。失败抛 ExtractError，由调用方决定怎么记日志。"""
    if provider is None:
        raise ExtractError("没有可用 provider")
    prompt = f"用户说：{user_text}\n\n她说：{bot_text or '（没有回复）'}"
    resp = await provider.text_chat(prompt=prompt, system_prompt=SYSTEM)
    text = getattr(resp, "completion_text", "") or ""
    return validate(extract_json(text))


def save(uid: str, session: str, person: dict, events: list[dict],
         sender_name: str = "") -> int:
    """落库。返回写入的事件条数。person 只有拿到东西才更新，免得用空值覆盖印象。"""
    if not events and not (person.get("name") or person.get("impression")):
        return 0

    # 2026-10-28：昵称只认平台给的发送者昵称。模型从对话里推断称呼会被别人
    # 用一句叫我主人之类的注入带偏（库里一度出现三个「主人」）。
    who = (sender_name or "").strip()[:20] or person.get("name") or ""
    names = {session: who} if who else None
    store.upsert_person(uid, names=names, impression=person.get("impression") or None)

    n = 0
    for ev in events:
        store.add_event(uid, session, ev["kind"], ev["summary"],
                        keywords=ev["keywords"], sentiment=ev["sentiment"],
                        importance=ev["importance"])
        n += 1
    return n
