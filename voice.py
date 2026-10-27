"""口吻接口：别的插件要"她的一句话"时，来这里取。

为什么要有这一层：同一张嘴在三个插件里说三种话，三天就露馅。
所以文案统一在这里生成，触发时机、名单、限速那些业务逻辑仍留在原插件——
那些代码是磨出来的，不该为了统一口吻去动。

用法（别的插件侧）：

    meta = context.get_registered_star("astrbot_plugin_life_memory")
    api = getattr(getattr(meta, "star_cls", None), "voice_api", None)
    if api:
        text = await api.say("birthday_ask", nickname="老张")
    if not text:
        text = "（原来的模板）"      # 拿不到就退回，绝不因为这边挂了而失声

四类文案，各有长度上限与禁忌。失败一律返回空字符串，由调用方兜底。
"""

from __future__ import annotations

import logging
import re

from . import persona, state, store

LOG = logging.getLogger("astrbot")

SPECS: dict[str, dict] = {
    "forward": {
        "max": 30,
        "brief": "发完说说之后，转发到群里时说的一句话",
        "banned": ("我发了", "请查收", "欢迎查看", "点击", "请多多关照"),
    },
    "birthday_greet": {
        "max": 55,
        "brief": "某个人今天过生日，对她说的一句话",
        "hint": ("要落到一件具体的东西上：她省下自己的白饭，用这些白饭做了个蛋糕给对方。"
                 "可以带一点不情愿（省饭是件心疼事），但东西是真的给了。不许客套。"),
        "banned": ("祝你生日快乐", "天天开心", "万事如意", "岁岁平安", "福如东海"),
    },
    "birthday_ask": {
        "max": 60,
        "brief": "私聊问对方生日日期时的开场白",
        "banned": ("请提供", "麻烦告知", "方便告知", "填写", "登记", "系统"),
    },
    "evening": {
        "max": 80,
        "brief": "睡前跟主人说一句今天过得怎么样",
        "hint": ("两三小句：今天做了什么（挑一件具体的）、心情如何、或者想起的一件小事。"
                 "不是总结，是随口讲。不提问、不道晚安模板、不许写成汇报。"),
        "banned": ("今天过得充实", "晚安好梦", "希望你", "总结", "汇报"),
    },
    "work_report": {
        "max": 40,
        "brief": "汇报一件已经做完的活",
        "banned": ("已完成", "处理完毕", "请您", "任务"),
    },
}

SYSTEM = """你在替一个角色说一句话。她叫大肥鱼，是住在自己小屋里的 AI 鲸鱼娘，
把 token 叫"白饭"。傲娇，懒，怕麻烦，嘴上不认，心里认。干活的是她的分身，她自己偷懒。

规则：
1. 只输出这一句话本身。不要引号、不要解释、不要写"她说："。
2. 像她随口说的，不像通知。不要客服腔、不要套话、不要祝福模板。
3. 不要自称"AI 助手""机器人"。
4. 可以带一点小情绪或不情愿，但该办的事要办到。"""


def clean(text: str, max_chars: int) -> str:
    t = (text or "").strip()
    t = re.sub(r"^(" + persona.prefix_pattern() + r")[：:]\s*", "", t)
    t = t.strip().strip("“”\"'「」『』").replace("\n", " ").strip()
    if len(t) > max_chars:
        t = t[:max_chars].rstrip("，,、；;")
    return t


def bad(text: str, kind: str) -> bool:
    spec = SPECS.get(kind) or {}
    if any(w in text for w in spec.get("banned", ())):
        return True
    # 客服腔通杀
    return any(w in text for w in ("需要我", "随时找我", "很高兴为您", "有什么可以帮"))


def build_prompt(kind: str, info: dict, session: str = "") -> str:
    spec = SPECS.get(kind, {})
    parts = [f"现在要说的类型：{spec.get('brief', kind)}",
             f"字数上限：{spec.get('max', 40)} 字"]
    if session:
        try:
            parts.append(state.build(session))
        except Exception:  # noqa: BLE001
            pass
    if spec.get("hint"):
        parts.append("写法提示：" + spec["hint"])
    for k, v in info.items():
        if v:
            parts.append(f"{k}：{v}")
    parts.append("写出她要说的那一句。")
    return "\n".join(parts)


async def say(provider, kind: str, info: dict | None = None, session: str = "") -> str:
    """生成一句话。失败、超限、命中禁忌，一律返回空。"""
    info = info or {}
    if provider is None or kind not in SPECS:
        return ""
    spec = SPECS[kind]
    try:
        resp = await provider.text_chat(prompt=build_prompt(kind, info, session),
                                        system_prompt=persona.system(SYSTEM))
        text = clean(getattr(resp, "completion_text", "") or "", spec["max"])
    except Exception as e:  # noqa: BLE001
        LOG.warning(f"[life_memory] 口吻生成失败（{kind}）：{e}")
        return ""
    if not text or bad(text, kind):
        LOG.info(f"[life_memory] 口吻被拦（{kind}）：{text[:30]}")
        return ""
    return text
