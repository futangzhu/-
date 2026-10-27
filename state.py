"""状态块：让她知道"此刻"。

M2 的全部内容。不改人格一个字，只是在系统提示词末尾补一小段现算的状态。

睡眠时间的唯一来源是 quiet_hours 插件的实物配置（主人 00:20 定）。
如果那边改了时间，这里自动跟着变，不需要改代码。
"""

from __future__ import annotations

import datetime
import json
import re
import os

from . import mood, paths, store, work

QUIET_HOURS_CFG = ""   # 运行时取 paths.quiet_hours_config()
FALLBACK = ("23:30", "07:00")   # 与 quiet_hours 出厂值一致，读不到配置时用
BUDGET_CHARS = 280

WEEKDAY = "一二三四五六日"


_QUIET_CACHE: dict = {"mtime": -1.0, "cfg": {}}


def quiet_config() -> dict:
    """读静默插件的配置。按文件 mtime 缓存，避免每条消息都去开文件。

    2026-10-26 04:02 修：这个插件的键是 `ranges: "23:30-07:00"`，
    不是 start_time / end_time。原来读不到字段，永远走兜底值，
    而兜底值恰好也是 23:30–07:00，所以从现象上看不出来。
    """
    import os

    path = paths.quiet_hours_config()
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if _QUIET_CACHE["mtime"] == mt:
        return _QUIET_CACHE["cfg"]
    try:
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    _QUIET_CACHE["mtime"] = mt
    _QUIET_CACHE["cfg"] = cfg
    return cfg


def sleep_window() -> tuple[str, str]:
    """(睡下, 醒来)。读 `ranges`，支持"23:30-07:00,12:00-13:00"这种多段写法（取第一段）。"""
    cfg = quiet_config()
    raw = str(cfg.get("ranges") or "").strip()
    m = re.match(r"(\d{1,2}:\d{2})\s*[-~至]\s*(\d{1,2}:\d{2})", raw)
    if m:
        return m.group(1), m.group(2)
    return FALLBACK


def quiet_disabled() -> bool:
    cfg = quiet_config()
    return cfg.get("enabled") is False


def awake_span() -> tuple[str, str]:
    """醒着的区间 (起, 止)。

    `sleep_window()` 返回的是"睡下、醒来"：("23:30","07:00")。
    名字容易看反——2026-10-26 00:40 就在 dayplan 里反着用了，被自检抓出来。
    """
    sleep_start, sleep_end = sleep_window()
    return sleep_end, sleep_start


def _hm(text: str) -> datetime.time:
    try:
        h, m = text.split(":")
        return datetime.time(int(h), int(m))
    except (ValueError, AttributeError):
        return datetime.time(0, 0)


def is_asleep(now: datetime.datetime | None = None) -> bool:
    if quiet_disabled():
        return False
    now = now or datetime.datetime.now(store.TZ)
    start, end = sleep_window()
    t, s, e = now.time(), _hm(start), _hm(end)
    if s <= e:
        return s <= t < e
    return t >= s or t < e          # 跨午夜


def _clock_word(now: datetime.datetime) -> str:
    h = now.hour
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 11:
        return "上午"
    if 11 <= h < 13:
        return "中午"
    if 13 <= h < 18:
        return "下午"
    if 18 <= h < 21:
        return "傍晚"
    if 21 <= h < 23:
        return "晚上"
    return "深夜"


def _last_speech_gap(session: str, now: datetime.datetime) -> str:
    row = store.last_speech(session)
    if not row:
        return ""
    try:
        last = store.to_local(row["ts"])
    except Exception:  # noqa: BLE001
        return ""
    mins = int((now - last).total_seconds() // 60)
    if mins < 1:
        return "你刚刚才说过话"
    if mins < 60:
        return f"你上一次说话是 {mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"你上一次说话是 {hours} 小时前"
    return "你今天还没在这个群说过话"


_CONDITIONAL = ("被人问", "有人问", "被问", "如果", "要是", "万一", "一旦", "问起")


def _strip_script(act: str) -> str:
    """去掉"被人问…就…"这类条件从句。

    2026-10-26 18:16 踩过：计划块里写着"在群里冒个泡，被人问周末过得怎样，糊弄过去"，
    主动开口时整句喂进去，模型把它当成已经发生的事，真替一个没人问的问题答了一句
    "周末啊……混过去就行了吧"。群里看着就是莫名其妙。
    """
    keep = [c.strip() for c in re.split(r"[，,；;]", act)
            if c.strip() and not any(w in c for w in _CONDITIONAL)]
    return "，".join(keep).strip()


def _current_activity(session: str, now: datetime.datetime, mode: str = "reply") -> str:
    """她已经选了细颗粒，那"现在具体在干嘛"就必须说出来——否则一追问就露。

    C 版唯一的额外成本就在这里：当前块也当状态喂进去。
    """
    blk = store.current_block(store.today_local(), now.strftime("%H:%M"))
    if not blk:
        # 计划块之间本来就有空隙（人不会无缝衔接）。留空的话她会自己编一个场景，
        # 编出来的东西第二天就对不上——给个平实的兜底，比留白安全。
        return "这会儿手上没什么事，就在屋里待着。"
    act = (blk.get("activity") or "").strip()
    place = (blk.get("place") or "").strip()
    if mode == "proactive":
        act = _strip_script(act)
        if not act and place:
            act = f"待在{place}"
    if not act:
        return ""
    # activity 本身常以"在…""待在…"开头，别加成"你现在在在群里"
    line = f"你现在{act}" if act.startswith(("在", "待", "正")) else f"你现在在{act}"
    if place:
        line += f"（{place}）"
    line += f"，这一块到 {blk.get('end_at')} 结束"
    hint = {"busy": "，手上正忙，不太想被打断",
            "idle": "，闲着的",
            "aware": "，手上有点事，但听得见"}.get(blk.get("attention"), "")
    return line + hint + "。"


def _sleep_begin_utc(now: datetime.datetime) -> str:
    """本次睡眠是从什么时候开始的（UTC 字符串）。跨午夜也要算对。"""
    sleep_start, _ = sleep_window()
    t = _hm(sleep_start)
    today_begin = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    begin = today_begin if now >= today_begin else today_begin - datetime.timedelta(days=1)
    return begin.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def wake_guidance(session: str, now: datetime.datetime) -> str:
    """睡着被叫的"程度"，不是台词。

    跟人被吵醒一样：第一次几乎没醒，多叫几次才慢慢起来，再叫就开始烦。
    这里只给程度和方向，具体说什么由她按人格决定——写死文案第二晚就会被复读成梗。
    """
    wakes = store.wakes_since(session, _sleep_begin_utc(now))
    n = len(wakes)
    if n == 0:
        return ""

    lead = ""
    if n >= 2:
        mins = int((now - store.to_local(wakes[-2]["ts"])).total_seconds() // 60)
        if mins >= 45:
            lead = "你刚才又睡着了。"
        elif mins <= 3:
            lead = "几乎是紧接着又叫。"

    if n == 1:
        core = "今晚第一次被叫。你基本没醒透，最多翻个身、出点动静。"
    elif n <= 3:
        core = f"今晚第 {n} 次被叫。你不太情愿，慢半拍，能少说就少说。"
    else:
        core = f"今晚第 {n} 次被叫。你有小脾气：拖长音、赖着不肯起、敲一下盆，但不许真发火，更不许顶撞。"

    return lead + core + "怎么反应你定，别把这句话当台词。"


def build(session: str, now: datetime.datetime | None = None, disturbed: bool = False,
          user_text: str = "", is_master: bool = False, mode: str = "reply") -> str:
    """拼出状态块。空字符串表示不注入。

    `disturbed`：这条消息是叫她（指令或被 @）。睡着被叫醒时不能还说"你还在睡"，
    否则她会一边回话一边声称自己在睡——2026-10-26 01:06 就是这么露的。
    """
    now = now or datetime.datetime.now(store.TZ)
    asleep = is_asleep(now)

    parts = [
        f"现在是 {now.hour:02d}:{now.minute:02d}，"
        f"{_clock_word(now)}，周{WEEKDAY[now.weekday()]}。"
    ]
    # 她的人格里"主人"是一个具体的人，但每次对话她只看到一个昵称。
    # 不点明这层，她就只能按"普通用户"对待——2026-10-26 主人反馈"不太认我这个身份"。
    if is_master:
        # 只写“正在说话的是主人”太软，模型会当背景信息。写成明确的称呼指示。
        parts.append("现在跟你说话的人就是主人本人，不是普通用户。"
                     "称呼他“主人”，或者叫“饭票”；不要叫“用户”。")

    if asleep and disturbed:
        parts.append("你本来在睡，这句是叫你的，被吵醒了。")
        guide = wake_guidance(session, now)
        if guide:
            parts.append(guide)
    elif asleep:
        parts.append("你还在睡（还没到起床的点）。")
    else:
        parts.append("你醒着。")

    if not asleep:
        act = _current_activity(session, now, mode=mode)
        if act:
            parts.append(act)

    if not asleep:
        try:
            m = mood.score(now)
            # 只在心情有明显偏向时才说。天天挂一句"这会儿平平的"，
            # 她开口就自带倦意——这是"说话变冷"的一个来源。
            if m["label"] not in ("平平的",):
                parts.append(f"这会儿{m['label']}。")
        except Exception:  # noqa: BLE001
            pass

    # 她"知道自己动过工作"，但不知道别的插件怎么实现的——这里只读一行结果
    try:
        busy = work.brief()
        if busy:
            parts.append(busy)
    except Exception:  # noqa: BLE001
        pass

    # 被问到"这谁弄的"时，给她当天真实做过的事，免得张口就编。
    # 只在问到工作类话题时注入，平时不塞——不然她每天张口就是"今天我干了…"
    try:
        if user_text and work.is_work_question(user_text):
            summary = work.summary()
            if summary:
                parts.append(summary + "。")
    except Exception:  # noqa: BLE001
        pass

    gap = _last_speech_gap(session, now)
    if gap:
        parts.append(gap + "。")

    today_n = store.today_speech_count(session, store.today_local())
    if today_n:
        parts.append(f"今天在这个群已经说了 {today_n} 句。")

    text = "【此刻】" + "".join(parts)
    return text[:BUDGET_CHARS]
