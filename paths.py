"""路径集中管理：不写死本机绝对路径，别人装上也能跑。

之前这些常量散在各文件里写死了 `/AstrBot/data/...`，一分享出去就会全线崩。
现在统一从这里取，规则：

    数据目录     get_astrbot_data_path()（AstrBot 的 data/）
    插件数据     data/plugin_data/astrbot_plugin_life_memory/
    配置目录     data/config/
    人格库       data/data_v4.db
    环境变量     LIFE_MEMORY_DB 仍可覆盖数据库路径（离线自检用）

任何一个取不到，都退回到"相对当前文件"的猜法，保证不至于直接抛异常。
"""

from __future__ import annotations

import os

PLUGIN_NAME = "astrbot_plugin_life_memory"


def _data_root() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        root = get_astrbot_data_path()
        # 2026-10-26 04:15：离线导入这个函数会返回插件自己的 data 目录，
        # 于是配置路径全错。加一道校验——真正的 data 根目录里一定有 config/。
        if root and os.path.isdir(os.path.join(str(root), "config")):
            return str(root)
    except Exception:  # noqa: BLE001
        pass
    # 退化：插件目录在 <data>/plugins/<name>，所以往上两层就是 data
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def data_root() -> str:
    return _data_root()


def plugin_dir() -> str:
    return os.path.join(data_root(), "plugin_data", PLUGIN_NAME)


def db_path() -> str:
    return os.environ.get("LIFE_MEMORY_DB") or os.path.join(plugin_dir(), "memory.db")


def prompt_file() -> str:
    return os.path.join(os.path.dirname(db_path()), "dayplan_prompt.txt")


def config_path() -> str:
    return os.path.join(data_root(), "config", f"{PLUGIN_NAME}_config.json")


def persona_db() -> str:
    return os.path.join(data_root(), "data_v4.db")


def quiet_hours_config() -> str:
    return os.path.join(data_root(), "config", "astrbot_plugin_quiet_hours_config.json")


def space_db() -> str:
    """发布插件的库。它不在就不发，不报错。"""
    return os.path.join(data_root(), "plugin_data", "astrbot_plugin_space_kit", "space.db")


def attachments_dir() -> str:
    return os.path.join(data_root(), "attachments")
