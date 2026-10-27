"""角色身份：名字、口头禅、一句身份描述。

2026-10-28：原来这四个提示词里把「大肥鱼 / AI 鲸鱼娘 / token 叫白饭」写死了，
别人装上就只能是作者那只。现在抽出来放配置，默认值还是原来那几个词。

只替换"称呼"层面的词，不重写角色性格（傲娇、懒那些写在提示词正文里，
那是这个插件自带的味道）。想整套换掉，改 `dayplan_prompt.txt` 那份文件，
或者直接改提示词——源码就在眼前。

`apply()` 在默认值时是**空操作**：替换前后的字符串完全一样，
所以现网那只大肥鱼开口不会有一个字的变化。

2026-10-28 追加：`persona_follow_astrbot`（默认关）。
打开以后，插件自己发起的那几次调用（抽记忆、主动开口、分身汇报、写日记、排日程）
会先把 AstrBot 当前人格的**前 400 字**拼到最前面，让"她是谁"跟聊天那边一致。
关着就完全自立，行为和 1.0.0 一模一样。
"""

from __future__ import annotations

import time

# 出厂值 = 作者自己的角色。配置留空就用这三个。
DEFAULT_NAME = "大肥鱼"
DEFAULT_CATCH = "白饭"
DEFAULT_DESC = ""

# 提示词里出现的两种身份句式（用来整体替换成自己的描述）
DESC_PATTERNS = (
    "住在机器里的 AI 鲸鱼娘",
    "住在自己小屋里的 AI 鲸鱼娘",
    "住在自己小屋里的 AI 鲸鱼娘，把 token 叫「白饭」",
)

NAME = DEFAULT_NAME
CATCH = DEFAULT_CATCH
DESC = DEFAULT_DESC

# ---- 跟随 AstrBot 人格（可选） ----
FOLLOW = False            # persona_follow_astrbot
HEAD_CHARS = 400          # 只截前 400 字：人格常有两三千字，全塞会把"只说一句话"的约束挤没
HEAD_TTL = 120            # 秒。人格可能在面板里换，别一辈子缓存
_CONTEXT = None
_HEAD = {"at": 0.0, "text": ""}


def set_context(ctx) -> None:
    """把 AstrBot 的 Context 交给本模块（取 persona_manager 用）。"""
    global _CONTEXT
    _CONTEXT = ctx


def set_from_config(cfg: dict | None) -> None:
    """从插件配置里读四项。缺项、空串都退回出厂值。"""
    global NAME, CATCH, DESC, FOLLOW
    cfg = cfg or {}
    name = str(cfg.get("persona_name") or "").strip()
    catch = str(cfg.get("persona_catch") or "").strip()
    desc = str(cfg.get("persona_desc") or "").strip()
    NAME = name[:16] or DEFAULT_NAME
    CATCH = catch[:8] or DEFAULT_CATCH
    DESC = desc[:60]
    FOLLOW = bool(cfg.get("persona_follow_astrbot"))
    _HEAD["at"] = 0.0     # 配置一变，缓存作废


def _squash(text: str) -> str:
    """压成一段：去多余空行、去 markdown 装饰，只留字。"""
    import re

    t = str(text or "")
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"[#*`>_~]+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _read_db_prompt() -> str:
    """退化路径：persona_manager 拿不到时，直接读人格库的 default 卡。"""
    try:
        import sqlite3

        from . import paths

        conn = sqlite3.connect(f"file:{paths.persona_db()}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT system_prompt FROM personas WHERE persona_id='default'"
            ).fetchone()
        finally:
            conn.close()
        return str((row[0] if row else "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _astrbot_head() -> str:
    """当前人格的前 400 字。没开、取不到、为空 → 空串（等于不跟随）。"""
    if not FOLLOW or _CONTEXT is None:
        return ""
    now = time.time()
    if now - _HEAD["at"] < HEAD_TTL:
        return _HEAD["text"]
    prompt = ""
    try:
        pm = getattr(_CONTEXT, "persona_manager", None)
        pid = str(getattr(pm, "default_persona", "") or "") if pm is not None else ""
        if pm is not None and pid:
            try:
                card = pm.get_persona_v3_by_id(pid)
            except Exception:  # noqa: BLE001
                card = None
            if isinstance(card, dict):
                prompt = str(card.get("prompt") or card.get("system_prompt") or "")
    except Exception:  # noqa: BLE001
        prompt = ""
    if not prompt:
        prompt = _read_db_prompt()
    text = _squash(prompt)[:HEAD_CHARS]
    _HEAD.update({"at": now, "text": text})
    return text


def system(text: str) -> str:
    """给提示词加"她是谁"的头（仅在开启跟随时），再做出厂词替换。

    头只负责"她是谁"；格式规则（"只输出一句话""不超过 25 字"）仍然以插件为准，
    因为那些规则写在 text 里，位置在后面——模型不会因为前面长了就无视后面。
    """
    body = apply(text)
    head = _astrbot_head()
    if not head:
        return body
    return f"【她是谁】{head}\n\n{body}"


def apply(text: str) -> str:
    """把提示词里的出厂称呼换掉。没配就是原样返回。"""
    if not text:
        return text
    out = text
    if NAME != DEFAULT_NAME:
        out = out.replace(DEFAULT_NAME, NAME)
    if CATCH != DEFAULT_CATCH:
        out = out.replace(DEFAULT_CATCH, CATCH)
    if DESC:
        for pat in DESC_PATTERNS:
            out = out.replace(pat, DESC)
    return out


def prefix_pattern() -> str:
    """清洗用的正则前缀：`她说|XX说|回复`，正则字符串，不含分组外的东西。"""
    import re

    names = {"她说", "回复"}
    names.add(f"{NAME}说")
    return "|".join(re.escape(n) for n in sorted(names))
