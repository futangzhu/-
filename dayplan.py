"""日计划：她每天的一天（M3）。

设计要点（样例-日计划.md 版本 C，主人 00:34 定）：
- 细颗粒 12–16 块，每块 ≥25 分钟，块间允许 0–25 分钟空隙
- 每天必须有 2–3 个 origin=random 的"临时起意"
- 醒睡区间不写死，从 quiet_hours 配置读
- 生成一次落库，当天不再重排（ensure_day 幂等）

生成失败时宁可没有计划，也不要塞一份坏计划——坏计划会让她一整天说错话。
"""

from __future__ import annotations

import datetime
import json
import os
import re

from . import persona, state, store

BLOCK_MIN = 12
BLOCK_MAX = 16
MIN_MINUTES = 25
MAX_GAP_MINUTES = 25
RANDOM_MIN, RANDOM_MAX = 2, 3
ATTENTION = {"idle", "aware", "busy"}

# 一句话原则（2026-10-26 01:07 主人指出）：
#   她知道自己是个住在机器里的 AI 鲸鱼娘 —— 跟"AI 自身"有关的词，是她的日常词汇，全部放行
#   不许出现的是"职场打工人"的词汇：她不上班，没人给她派活、没人跟她开会
OFFICE_SPEAK = ("项目", "任务", "会议", "报表", "汇报", "KPI", "上班", "下班",
                "加班", "值班", "业绩", "审批", "排期", "述职")

# AI 自身的词：明确允许，写在这里是给后来的人看，不是用来拦的
AI_TERMS_OK = ("模型", "算力", "白饭", "上下文", "掉线", "参数", "显存",
               "接口", "服务器", "日志", "监控", "部署", "训练", "推理")

# 公文腔：不是身份问题，是说话方式问题（"用餐""进行"这些她不会说）
BUREAU_SPEAK = ("处理事务", "进行", "完成工作", "休息时间", "用餐", "活动")

# 条件剧本：activity 是"她正在做什么"，不是"如果有人问就怎么答"。
# 2026-10-26 18:16："在群里冒个泡，被人问周末过得怎样，糊弄过去"被喂进主动开口，
# 模型当成已发生的事，真答了一句没人问的问题。
CONDITIONAL = ("被人问", "有人问", "被问", "如果", "要是", "万一", "一旦", "问起")

# 鱼缸世界：她是人（鲸鱼娘），不住水缸、不游来游去。尾巴是长相，不是生活方式
FISH_WORDS = ("缸", "游")

SYSTEM = """你在替一个角色规划"平凡的一天"。

角色：一个被主人从模型里拽出来、养在机器里的姑娘。
她是鲸鱼娘——普通女孩的底子，多了鲸鱼鳍造型的耳朵和一条鲸鱼尾，除此之外和别的女孩没什么两样。
傲娇，怕麻烦但嘴硬。她把 token 叫"白饭"——token 是设定里的词，白饭是她嘴上的词。

注意：她不是鱼，不住水缸、不泡在水里、不靠甩尾巴生活。
那条尾巴只是长相的一部分，大部分时候她根本想不起来自己有。

要求：
1. 只输出 JSON，不要解释，不要代码块标记以外的任何文字。
2. 结构：{"mood": "一句话心情", "blocks": [...]}
3. blocks 依次是：
   {"start":"HH:MM","end":"HH:MM","activity":"不超过 20 字，用她会用的说法","place":"小屋/床上/桌前/窗边/街上/群里","attention":"idle|aware|busy","energy":0-100,"origin":"plan|random"}
4. 块数 12–16。每块至少 25 分钟。相邻块之间可以留 0–25 分钟空档（人不会无缝衔接）。
5. 醒来那一刻就是第一块的开始，睡着前那一刻是最后一块的结束。
6. 必须有 2–3 块 origin 是 "random"，代表没来由的临时起意（比如回笼觉没睡成、
   查一个没头没尾的问题、被窗外的东西吸走二十分钟）。random 块也要具体，不要写成"发呆一会"。
7. 工作日的"忙"是那种不情愿的忙；周末更散、更容易出门或闲逛。
8. activity 里不要出现"处理""进行""安排""用餐"这类公文词。
8b. activity 只写她正在做什么，不写条件和应对：不要"被人问…就…""如果有人…""万一…"。
    想写跟人说话，写成情景——"在群里冒个泡，听两句就走"，不要写成剧本。
9. mood 是一句话，不要写成情绪标签。
10. 她不上班，没有人给她派活，也没有 KPI。白饭是吃的，是补算力用的，
    不是工作内容，更不是"泡在里面干活"的地方。
11. 她知道自己是个住在机器里的 AI 鲸鱼娘，所以"算力、模型、上下文、掉线、参数、
    接口、服务器、日志、监控、部署、训练"这些词都属于她的日常，可以正常用。
11b. 但她不上班：禁止出现项目、任务、会议、报表、汇报、KPI、上班、下班、加班、值班、
    排期、业绩这类词。没有人给她派活，她做的每件事都是自己想做的。
11d. 白饭可以多用，大家都爱看；但它是 token 的口语，不许写成"塞 token 当白饭"这种把两个词并列的句子。
11c. 也不许写"水缸""缸底""缸壁""游来游去"这类——她住在房间里，不是养在缸里。
    尾巴是长相的一部分，不是交通工具。
12. 至少 2 块是"什么都没干"（赖床、发呆、趴着、装睡、盯着天花板），
    单块不少于 40 分钟——人一天里有很多时间是没有名字的，别排满。
13b. 至少 2 块是"有精神的事"：出门、折腾一样东西、跟人说话、突然想干点什么。
    整天赖在床上会让她说话也带倦意。
13. attention=busy 只给她自己入迷的事（翻旧账、琢磨没用的东西、把一个东西拆了装不回去），
    不能是工作。整天的 busy 不要超过 4 块。

只输出 JSON。"""


class PlanError(ValueError):
    """计划不可用。带原因，方便回喂给模型重试一次。"""


LAST_ERROR = ""


def _hm(text: str) -> int:
    """HH:MM → 当天第几分钟。"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m:
        raise PlanError(f"时间格式不对：{text!r}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise PlanError(f"时间越界：{text!r}")
    return h * 60 + mi


def _fmt(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。容忍 ```json 围栏和前后废话。"""
    raw = (text or "").strip()
    if not raw:
        raise PlanError("模型返回空")
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start, depth, in_str, esc = -1, 0, False, False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if start < 0:
            if ch == "{":
                start, depth = i, 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except ValueError as e:
                    raise PlanError(f"JSON 解析失败：{e}") from e
    raise PlanError("输出里找不到完整的 JSON 对象")


def validate(plan: dict, day: datetime.date | None = None) -> list[str]:
    """返回问题清单。空列表表示可用。"""
    issues: list[str] = []
    blocks = plan.get("blocks")
    if not isinstance(blocks, list):
        return ["blocks 不是数组"]
    if not (BLOCK_MIN <= len(blocks) <= BLOCK_MAX):
        issues.append(f"块数 {len(blocks)}，要求 {BLOCK_MIN}–{BLOCK_MAX}")

    wake, sleep = state.awake_span()
    want_start, want_end = _hm(wake), _hm(sleep)

    prev_end = None
    random_n = 0
    for i, b in enumerate(blocks):
        tag = f"第{i + 1}块"
        if not isinstance(b, dict):
            issues.append(f"{tag} 不是对象")
            continue
        try:
            s, e = _hm(b.get("start")), _hm(b.get("end"))
        except PlanError as err:
            issues.append(f"{tag} {err}")
            continue
        if e <= s:
            issues.append(f"{tag} 结束不晚于开始：{b.get('start')}–{b.get('end')}")
        if e - s < MIN_MINUTES:
            issues.append(f"{tag} 只有 {e - s} 分钟，太碎")
        if prev_end is not None:
            if s < prev_end:
                issues.append(f"{tag} 与前一块重叠")
            elif s - prev_end > MAX_GAP_MINUTES:
                issues.append(f"{tag} 前有空档 {s - prev_end} 分钟，太久了")
        prev_end = e

        if i == 0 and abs(s - want_start) > 10:
            issues.append(f"第一块从 {b.get('start')} 开始，但醒点是 {wake}")
        if i == len(blocks) - 1 and abs(e - want_end) > 10:
            issues.append(f"最后一块到 {b.get('end')} 结束，但睡点是 {sleep}")

        if b.get("attention") not in ATTENTION:
            issues.append(f"{tag} attention 非法：{b.get('attention')!r}")
        energy = b.get("energy")
        if not isinstance(energy, int) or not (0 <= energy <= 100):
            issues.append(f"{tag} energy 非法：{energy!r}")
        act = str(b.get("activity") or "")
        if not act:
            issues.append(f"{tag} activity 为空")
        elif len(act) > 24:
            issues.append(f"{tag} activity 太长（{len(act)} 字）")
        for w in BUREAU_SPEAK:
            if w in act:
                issues.append(f"{tag} 说成了公文腔「{w}」")
        for w in CONDITIONAL:
            if w in act:
                issues.append(f"{tag} activity 写成条件剧本了「{w}」——只写她正在做什么")
        for w in OFFICE_SPEAK:
            if w in act:
                issues.append(f"{tag} 出现了上班族的词「{w}」（她不上班）")
        for w in FISH_WORDS:
            if w in act and not (w == "游" and "游戏" in act):
                issues.append(f"{tag} activity 把她写成鱼了：「{w}」")
        if b.get("origin") == "random":
            random_n += 1
        elif b.get("origin") != "plan":
            issues.append(f"{tag} origin 非法：{b.get('origin')!r}")

    if not (RANDOM_MIN <= random_n <= RANDOM_MAX):
        issues.append(f"random 块 {random_n} 个，要求 {RANDOM_MIN}–{RANDOM_MAX}")
    if not str(plan.get("mood") or "").strip():
        issues.append("缺 mood")
    return issues


def parse_plan(text: str) -> dict:
    plan = extract_json(text)
    issues = validate(plan)
    if issues:
        raise PlanError("；".join(issues[:6]))
    return plan


def build_prompt(day: datetime.date) -> str:
    wake, sleep = state.awake_span()
    wd = "一二三四五六日"[day.weekday()]
    kind = "工作日" if day.weekday() < 5 else "周末"
    return (f"日期：{day.isoformat()}，周{wd}，{kind}。\n"
            f"醒着的区间：{wake} 到 {sleep}。\n"
            f"请给出这天的计划 JSON。")


def save_day(day: str, plan: dict) -> None:
    ts = store.now_utc()
    with store.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO life_day (day, plan, mood, energy, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (day, json.dumps(plan, ensure_ascii=False), str(plan.get("mood") or ""),
             int(plan.get("blocks", [{}])[0].get("energy", 60) or 60), ts, ts),
        )
        conn.execute("DELETE FROM life_block WHERE day=?", (day,))
        for b in plan["blocks"]:
            conn.execute(
                "INSERT INTO life_block (day, start_at, end_at, activity, place, attention,"
                " energy, origin, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (day, b["start"], b["end"], b["activity"], b.get("place", ""),
                 b.get("attention", "idle"), int(b.get("energy", 60)),
                 b.get("origin", "plan"), ts),
            )


def load_day(day: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM life_day WHERE day=?", (day,)).fetchone()
        if not row:
            return None
        blocks = [dict(r) for r in conn.execute(
            "SELECT * FROM life_block WHERE day=? ORDER BY start_at", (day,))]
    return {"day": day, "mood": row["mood"], "plan": json.loads(row["plan"]), "blocks": blocks}


def render_plan(loaded: dict) -> str:
    """给人看的一天。`·` 标出临时起意那块，方便一眼看出像不像活人。"""
    if not loaded:
        return "今天还没有计划"
    lines = [f"{loaded['day']}　{loaded.get('mood') or ''}".rstrip()]
    for b in loaded.get("blocks", []):
        mark = "·" if b.get("origin") == "random" else " "
        lines.append(f"{b['start_at']}–{b['end_at']}{mark}{b['activity']}")
    return "\n".join(lines)


def ensure_prompt_file() -> str:
    """首次运行时把内置提示词落到文件，之后以文件为准。"""
    path = store.prompt_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(persona.apply(SYSTEM))
    except OSError:
        pass
    return path


def system_prompt() -> str:
    """提示词放文件里，改它不用重载插件。

    动机：调提示词是高频动作，每改一次就重启进程太重，
    而且重启会打断正在跑的对话。
    """
    path = store.prompt_file()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return persona.apply(SYSTEM)


async def ensure_day(provider, day: datetime.date | None = None, force: bool = False) -> dict | None:
    """当天有计划就直接用；没有就生成一次。失败返回 None，不写坏数据。"""
    global LAST_ERROR
    LAST_ERROR = ""
    day = day or datetime.datetime.now(store.TZ).date()
    key = day.isoformat()
    existing = load_day(key)
    if existing and not force:
        return existing
    if provider is None:
        return None

    prompt = build_prompt(day)
    last_err = ""
    for attempt in range(2):
        ask = prompt if not last_err else f"{prompt}\n\n上一次没通过，问题：{last_err}\n请修正后重新只输出 JSON。"
        try:
            resp = await provider.text_chat(prompt=ask, system_prompt=persona.system(system_prompt()))
            text = getattr(resp, "completion_text", "") or ""
            plan = parse_plan(text)
        except PlanError as e:
            last_err = str(e)
            continue
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue
        save_day(key, plan)
        return load_day(key)

    LAST_ERROR = last_err
    return None
