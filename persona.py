"""角色身份：名字、口头禅、一句身份描述。

2026-10-28：原来这四个提示词里把「大肥鱼 / AI 鲸鱼娘 / token 叫白饭」写死了，
别人装上就只能是作者那只。现在抽出来放配置，默认值还是原来那几个词。

只替换"称呼"层面的词，不重写角色性格（傲娇、懒那些写在提示词正文里，
那是这个插件自带的味道）。想整套换掉，改 `dayplan_prompt.txt` 那份文件，
或者直接改提示词——源码就在眼前。

`apply()` 在默认值时是**空操作**：替换前后的字符串完全一样，
所以现网那只大肥鱼开口不会有一个字的变化。
"""

from __future__ import annotations

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


def set_from_config(cfg: dict | None) -> None:
    """从插件配置里读三项。缺项、空串都退回出厂值。"""
    global NAME, CATCH, DESC
    cfg = cfg or {}
    name = str(cfg.get("persona_name") or "").strip()
    catch = str(cfg.get("persona_catch") or "").strip()
    desc = str(cfg.get("persona_desc") or "").strip()
    NAME = name[:16] or DEFAULT_NAME
    CATCH = catch[:8] or DEFAULT_CATCH
    DESC = desc[:60]


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
