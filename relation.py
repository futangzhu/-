"""关系分档：把 relation 数值翻译成人话。

档位表借用 `astrbot_plugin_self_learning` 的 `affection_scale.py`（同一套描述词，
同一个人在两个插件里的"分量"说的是一种话），但**数值由本插件自己维护**。

为什么不直接读它的数：单一写入方是项目红线。她要不要主动找人，如果取决于别人维护的
数值，两边规则一改就会打架（"对他好感 300，relation 却是 12"听谁的？）。
这套插件以后要能单独分享出去，多一个依赖就少一分可移植性。
真要统一，以后加个适配器读它的值就行。
"""

from __future__ import annotations

# (下限, 描述)，从高到低匹配
TIERS: list[tuple[int, str]] = [
    (800, "深度依赖"),
    (500, "亲密无间"),
    (300, "相当喜欢"),
    (180, "很喜欢"),
    (80, "非常喜欢"),
    (60, "比较喜欢"),
    (40, "一般好感"),
    (20, "略有好感"),
    (0, "初次见面"),
    (-20, "略有反感"),
    (-40, "比较反感"),
    (-60, "相当讨厌"),
    (-80, "非常讨厌"),
]

LOWEST_DESC = "极度厌恶"

# 行为门槛：到这个档位才会主动找他说话
SPEAK_THRESHOLD = 60          # 比较喜欢
STEP = 2                      # 每聊一次 +2
CAP = 800


def describe(level: int) -> str:
    for threshold, desc in TIERS:
        if level >= threshold:
            return desc
    return LOWEST_DESC
