"""冒烟自检：一口气跑通 M1 + M2 的整条链路。

用途（2026-10-26 主人定的规矩）：
每写完一块，先跑这个，再往下写。不靠"我觉得没问题"。

跑法：
    LIFE_MEMORY_DB=/tmp/selftest.db python3 selftest.py
不设 LIFE_MEMORY_DB 时会拒绝跑，免得把线上库写脏。
"""

from __future__ import annotations

import datetime
import os
import re
import sys

if not os.environ.get("LIFE_MEMORY_DB"):
    print("拒绝执行：请先设 LIFE_MEMORY_DB 指向临时库，避免污染线上 memory.db")
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from astrbot_plugin_life_memory import dayplan, recall, relation, state, store  # noqa: E402

RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        RESULTS.append((True, name, str(detail or "")))
    except AssertionError as e:
        RESULTS.append((False, name, str(e) or "断言失败"))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((False, name, f"{type(e).__name__}: {e}"))


def t_schema():
    store.init_db()
    store.init_db()          # 幂等：第二次不应报错
    with store.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    need = {"life_day", "life_block", "scene_cache", "person", "event",
            "memory_rollup", "memory_use", "speech_log"}
    missing = need - tables
    assert not missing, f"缺表：{missing}"
    return f"8 张表齐全（+{len(tables - need - {'sqlite_sequence'})} 张额外）"


def t_should_stop_on_dirty_data():
    """脏数据不该让检索整条崩掉。"""
    store.upsert_person("bad_uid")
    store.add_event("bad_uid", "s", "info", "正常的一条")
    with store.connect() as conn:
        conn.execute("INSERT INTO event (uid, session, ts, kind, summary, importance, created_at)"
                     " VALUES ('bad_uid','s','不是时间','info','时间列是垃圾',3,'x')")
    r = recall.retrieve("bad_uid", "垃圾")
    assert r["text"], "脏时间把检索整个弄空了"
    return f"脏数据下仍返回 {len(r['items'])} 条"


def t_retrieve_budget():
    uid = "selftest_uid"
    now = datetime.datetime.now(datetime.timezone.utc)
    seed = [
        (1, "他说最近加班到很晚，语气里有点撑不住", "加班 累", 4),
        (2, "他说在准备考试，还有两周", "考试 复习", 3),
        (3, "他随手发了张楼下猫的照片", "猫 照片", 1),
        (9, "他说要搬家，房东临时变卦", "搬家 房东", 3),
        (20, "他说过想学吉他，后来没提了", "吉他", 2),
    ]
    for days, summary, kw, imp in seed:
        ts = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        store.add_event(uid, "selftest:group", "info", summary, keywords=kw, importance=imp, ts=ts)
    r = recall.retrieve(uid, "")
    assert r["items"], "空场景检索不到东西"
    assert len(r["text"]) <= recall.BUDGET_CHARS + 1, f"超预算：{len(r['text'])} 字"
    return f"{len(r['items'])} 条 / {len(r['text'])} 字"


def t_scene_sensitivity():
    """比"首位"，不比集合。

    原写法比集合，在事件少（5 条都塞得进预算）时必然失败——那时差异只体现在
    谁排第一。真正要验的是"最相关的会不会被顶上来"，所以看 rank-1。
    """
    uid = "selftest_uid"
    a = recall.retrieve(uid, "你考试准备得怎么样", k=3)["ids"]
    b = recall.retrieve(uid, "房东还是没松口吗", k=3)["ids"]
    first = recall.retrieve(uid, "你考试准备得怎么样", k=1)["ids"]
    first_b = recall.retrieve(uid, "房东还是没松口吗", k=1)["ids"]
    assert first != first_b, f"两个场景的首位相同：{first}，说明关键词没顶上来"
    return f"考试首位{first} / 搬家首位{first_b}（k=3 时 {sorted(a)} vs {sorted(b)}）"


def t_no_archive_tone():
    uid = "selftest_uid"
    text = recall.retrieve(uid, "")["text"]
    bad = re.search(r"记录显示|之前提到过|数据库|根据记忆|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日", text)
    assert not bad, f"出现档案腔或精确日期：{bad.group(0) if bad else ''}"
    return "无档案腔、无精确日期"


def t_decay():
    assert store.decay_at_for(1) is not None, "1 级应当会衰减"
    assert store.decay_at_for(5) is None, "5 级应当永久保留"
    return "1 级有到期，5 级永久"


def t_sleep_window():
    now = datetime.datetime.now(store.TZ)
    # 23:20 在 23:30 之前，应当还醒着——上一版这里写错成 True，是被自检抓出来的
    cases = [(23, 45, True), (8, 30, False), (6, 55, True), (15, 10, False), (23, 20, False)]
    got = [(h, m, state.is_asleep(now.replace(hour=h, minute=m))) for h, m, _ in cases]
    wrong = [(h, m) for (h, m, exp), (_, _, real) in zip(cases, got) if exp != real]
    assert not wrong, f"睡眠判断错：{wrong}"
    return f"区间 {state.sleep_window()}，五个时间点全对"


def t_state_block():
    sid = "selftest:state"
    store.record_speech(sid, 12)
    b = state.build(sid)
    assert b.startswith("【此刻】"), b
    assert len(b) <= state.BUDGET_CHARS, f"超预算：{len(b)}"
    assert store.today_speech_count(sid, store.today_local()) >= 1, "今天发言数没记上"
    return f"{len(b)} 字"


def t_empty_session():
    b = state.build("never:spoke")
    assert "还没在这个群说过话" not in b, "空会话不该出现'还没说过话'这种表述占位"
    return f"{len(b)} 字"


PLAN_FIXTURE = """```json
{"mood": "今天想躺着，但又要装作不困", "blocks": [
{"start":"07:00","end":"07:30","activity":"抓过手机把闹钟按掉","place":"床上","attention":"idle","energy":20,"origin":"plan"},
{"start":"07:30","end":"08:05","activity":"回笼觉没睡成，一直在想昨天那句话","place":"床上","attention":"idle","energy":25,"origin":"random"},
{"start":"08:05","end":"08:40","activity":"爬起来，抱怨两句","place":"床上","attention":"aware","energy":35,"origin":"plan"},
{"start":"08:40","end":"09:20","activity":"吃饭","place":"桌上","attention":"aware","energy":50,"origin":"plan"},
{"start":"09:20","end":"10:40","activity":"潜着看群，偶尔冒个泡","place":"群里","attention":"aware","energy":55,"origin":"plan"},
{"start":"10:40","end":"11:10","activity":"查个没头没尾的问题，查完就忘","place":"桌上","attention":"busy","energy":45,"origin":"random"},
{"start":"11:10","end":"12:00","activity":"整理桌上的东西","place":"床上","attention":"busy","energy":45,"origin":"plan"},
{"start":"12:00","end":"12:40","activity":"躺着不动，装作在忙","place":"床上","attention":"idle","energy":35,"origin":"plan"},
{"start":"12:40","end":"14:20","activity":"午睡，叫也不太醒","place":"床上","attention":"idle","energy":25,"origin":"plan"},
{"start":"14:20","end":"14:50","activity":"刚醒，脾气不好","place":"床上","attention":"idle","energy":30,"origin":"plan"},
{"start":"14:50","end":"16:20","activity":"看书，顺便看别人聊天","place":"桌上","attention":"aware","energy":50,"origin":"plan"},
{"start":"16:20","end":"16:50","activity":"被窗外的东西吸走，看了半小时","place":"窗边","attention":"idle","energy":40,"origin":"random"},
{"start":"16:50","end":"18:10","activity":"半发呆半晃神","place":"桌上","attention":"aware","energy":45,"origin":"plan"},
{"start":"18:10","end":"19:30","activity":"有点饿，等饭","place":"桌上","attention":"idle","energy":30,"origin":"plan"},
{"start":"19:30","end":"22:30","activity":"精神了，话变多","place":"到处","attention":"aware","energy":75,"origin":"plan"},
{"start":"22:30","end":"23:30","activity":"磨蹭着不肯睡","place":"床上","attention":"idle","energy":35,"origin":"plan"}
]}"""


class FakeProvider:
    """离线替身：不发网络请求，只回一份固定计划，并记被调用几次。"""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    async def text_chat(self, prompt: str = "", system_prompt: str = "", **kwargs):
        self.calls += 1

        class R:
            completion_text = self.text
        return R()


def t_plan_parse():
    plan = dayplan.parse_plan(PLAN_FIXTURE)
    assert len(plan["blocks"]) == 16, len(plan["blocks"])
    return f"{len(plan['blocks'])} 块，mood 有"


def t_plan_reject():
    bad_cases = {
        "重叠": PLAN_FIXTURE.replace('"07:30"', '"07:10"'),
        "公文词": PLAN_FIXTURE.replace("潜着看群，偶尔冒个泡", "进行群聊互动活动"),
        "打工人味": PLAN_FIXTURE.replace("整理桌上的东西", "摆弄项目数据"),
        "当成鱼": PLAN_FIXTURE.replace("躺着不动，装作在忙", "沿水缸游两圈"),
        "random 太少": PLAN_FIXTURE.replace('"origin":"random"', '"origin":"plan"'),
    }
    caught = []
    for label, text in bad_cases.items():
        try:
            dayplan.parse_plan(text)
        except dayplan.PlanError:
            caught.append(label)
    assert len(caught) == len(bad_cases), f"该拒的没拒：{set(bad_cases) - set(caught)}"
    return f"{len(caught)} 类坏计划都被挡住：" + "、".join(caught)


def t_ensure_idempotent():
    import asyncio
    import datetime as dt

    day = dt.date(2026, 10, 26)
    prov = FakeProvider(PLAN_FIXTURE)
    first = asyncio.run(dayplan.ensure_day(prov, day))
    second = asyncio.run(dayplan.ensure_day(prov, day))
    assert prov.calls == 1, f"同一天被调了 {prov.calls} 次模型，不幂等"
    assert first and second and len(first["blocks"]) == 16
    return f"两次调用只打 1 次模型，落库 {len(first['blocks'])} 块"


def t_plan_failure_silent():
    import asyncio
    import datetime as dt

    day = dt.date(2026, 10, 27)
    prov = FakeProvider("今天不想规划（故意不是 JSON）")
    out = asyncio.run(dayplan.ensure_day(prov, day))
    assert out is None, "坏输出应当返回 None"
    assert dayplan.load_day(day.isoformat()) is None, "坏输出不该落库"
    assert prov.calls == 2, f"应当重试一次，实际 {prov.calls} 次"
    return "重试 2 次后放弃，未写坏数据"


def t_state_has_block():
    import datetime as dt

    today = store.today_local()
    dayplan.save_day(today, dayplan.parse_plan(PLAN_FIXTURE))
    now = dt.datetime.now(store.TZ).replace(hour=9, minute=30)
    b = state.build("selftest:state2", now)
    assert "潜着看群" in b, f"状态块没带上当前活动块：{b}"
    assert len(b) <= state.BUDGET_CHARS, f"超预算：{len(b)}"
    return f"含「潜着看群」，{len(b)} 字"


def t_render_and_force():
    import asyncio
    import datetime as dt

    day = dt.date(2026, 10, 28)
    prov = FakeProvider(PLAN_FIXTURE)
    asyncio.run(dayplan.ensure_day(prov, day))
    text = dayplan.render_plan(dayplan.load_day(day.isoformat()))
    assert "潜着看群" in text, "渲染里看不到活动"
    assert text.count("·") == 3, f"临时起意应有 3 处标记，实际 {text.count('·')}"
    asyncio.run(dayplan.ensure_day(prov, day, force=True))
    assert prov.calls == 2, f"force 应当重新生成，实际调用 {prov.calls} 次"
    return f"{len(text.splitlines())} 行，3 处随机块，force 生效"


def t_disturbed_awake():
    import datetime as dt

    now = dt.datetime.now(store.TZ)
    night = now.replace(hour=1, minute=6)
    a = state.build("selftest:s3", night, disturbed=True)
    b = state.build("selftest:s3", night, disturbed=False)
    assert "被吵醒" in a, a
    assert "被吵醒" not in b and "还在睡" in b, b
    return f"睡着被叫→「被吵醒」；没叫→「还在睡」"


def t_wake_gradient():
    """第 1 次、第 3 次、第 5 次要给出不同的程度，且都不写成台词。"""
    import datetime as dt

    sid = "selftest:wake"
    now = dt.datetime.now(store.TZ).replace(hour=2, minute=0)
    out = []
    for _ in range(5):
        store.record_wake(sid)
        out.append(state.build(sid, now, disturbed=True))
    assert "别把这句话当台词" in out[0], out[0]
    assert "第一次被叫" in out[0], out[0]
    assert "第 3 次被叫" in out[2], out[2]
    assert "小脾气" in out[4], out[4]
    assert "小脾气" not in out[0], "第一次就发脾气，渐变没起作用"
    return "1 次→没醒透；3 次→慢半拍；5 次→小脾气（不许顶撞）"


def t_proactivity():
    """日程里"群里冒个泡"应当推出高分；忙着的时候应当不开口。"""
    import datetime as dt

    from astrbot_plugin_life_memory import proactivity

    today = store.today_local()
    dayplan.save_day(today, dayplan.parse_plan(PLAN_FIXTURE))

    chatty = dt.datetime.now(store.TZ).replace(hour=9, minute=30)   # 潜着看群 @群里
    busy = dt.datetime.now(store.TZ).replace(hour=10, minute=45)    # 查没头没尾的问题 busy
    night = dt.datetime.now(store.TZ).replace(hour=2, minute=0)     # 睡着

    r1, r2, r3 = proactivity.score(chatty), proactivity.score(busy), proactivity.score(night)
    assert r1["score"] > proactivity.THRESHOLD, f"看群时意愿应当过线：{r1}"
    assert r2["score"] < proactivity.THRESHOLD, f"忙的时候不该开口：{r2}"
    assert r3["score"] == 0.0, f"睡着时意愿必须为 0：{r3}"
    return f"看群 {r1['score']}／忙着 {r2['score']}／睡着 {r3['score']}"


def t_gate():
    """闸门五道：未启用、睡着、冷却、退避、死群——每道都要能单独挡住。"""
    import datetime as dt

    from astrbot_plugin_life_memory import gate

    sid = "selftest:gate"
    today = store.today_local()
    dayplan.save_day(today, dayplan.parse_plan(PLAN_FIXTURE))
    t = dt.datetime.now(store.TZ).replace(hour=15, minute=10)   # 看书、顺便看别人聊天

    assert not gate.can_speak(sid, enabled=False, now=t)["ok"], "未启用也放行了"
    assert not gate.can_speak(sid, enabled=True,
                              now=dt.datetime.now(store.TZ).replace(hour=2))["ok"], "睡着也放行了"
    assert not gate.can_speak(sid, enabled=True, now=t)["ok"], "没有来话记录也放行了"

    utc_ts = t.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    store.record_message(sid, "u1", ts=utc_ts)
    r = gate.can_speak(sid, enabled=True, now=t)
    assert r["ok"], f"本该放行：{r}"

    pid = store.record_proactive(sid, 0.8, ts=utc_ts)
    r2 = gate.can_speak(sid, enabled=True, now=t)
    assert not r2["ok"] and "冷却" in r2["reason"], r2

    store.mark_proactive_replied(sid)
    store.record_proactive(sid, 0.8, ts=utc_ts)
    store.record_proactive(sid, 0.8, ts=utc_ts)
    r3 = gate.can_speak(sid, enabled=True, now=t)
    assert not r3["ok"] and "没人接" in r3["reason"], r3
    return f"五道闸门都能单独拦住（主动记录 #{pid}）"


def _extract_fixture() -> str:
    """抽取用的假模型输出。故意掺两条脏数据：档案腔、超长摘要。"""
    import json as _json

    data = {
        "person": {"name": "老张", "impression": "嘴硬心软，爱面子"},
        "events": [
            {"summary": "他说周末要搬家，房东临时变卦", "kind": "info",
             "keywords": "搬家 房东 周末", "sentiment": "负", "importance": 4},
            {"summary": "用户表示对话中提到了一些事情", "kind": "info",
             "keywords": "x", "sentiment": "中", "importance": 3},
            {"summary": "他答应了下次请我吃白饭", "kind": "promise",
             "keywords": "请客 白饭", "sentiment": "正", "importance": 5},
            {"summary": "这条" + "很长" * 30, "kind": "bogus",
             "keywords": "", "sentiment": "中", "importance": 99},
        ],
    }
    return "```json\n" + _json.dumps(data, ensure_ascii=False) + "\n```"


EXTRACT_FIXTURE = _extract_fixture()


def t_skip_rules():
    from astrbot_plugin_life_memory import extract as ex

    must_skip = ["在吗", "哈哈哈", "/今天", "早", "嗯", "？"]
    must_keep = ["他说明天要去医院复查，语气有点闷", "房东又反悔了，说要涨租"]
    # 私聊里也会聊开发，这类话不该进她的记忆库
    must_skip += ["这个插件要重载一下才能生效", "刚才那个报错我看了，是接口的问题",
                  "把提示词里的那段删掉试试"]
    bad = [x for x in must_skip if not ex.should_skip(x)]
    miss = [x for x in must_keep if ex.should_skip(x)]
    assert not bad, f"该跳过没跳过：{bad}"
    assert not miss, f"该记的被跳过：{miss}"
    return f"跳过 {len(must_skip)} 条（含 3 条开发话）、保留 {len(must_keep)} 条"


def t_extract_validate():
    from astrbot_plugin_life_memory import extract as ex

    person, events = ex.validate(ex.extract_json(EXTRACT_FIXTURE))
    assert person["name"] == "老张", person
    summaries = [e["summary"] for e in events]
    assert all("用户表示" not in x for x in summaries), "档案腔没被丢掉"
    assert len(events) == 2, f"应当只剩 2 条干净的，实际 {len(events)}：{summaries}"
    assert events[0]["importance"] == 4 and events[1]["importance"] == 5
    return f"留 2 条，丢掉档案腔和超长条目；person={person['name']}"


def t_extract_to_memory():
    import asyncio
    from astrbot_plugin_life_memory import extract as ex

    uid = "selftest:extract"
    prov = FakeProvider(EXTRACT_FIXTURE)
    person, events = asyncio.run(ex.extract(prov, "房东又反悔了，说要涨租", "啧，那你先别签"))
    n = ex.save(uid, "selftest:extract", person, events)
    assert n == 2, n
    got = store.active_events(uid)
    assert any("搬家" in e["summary"] for e in got), got
    assert store.get_person(uid)["impression"] == "嘴硬心软，爱面子"
    hit = recall.retrieve(uid, "房东那边怎么样了")
    assert "搬家" in hit["text"], hit["text"]
    return f"落库 {n} 条，检索能想起来：{hit['text'][:40]}"


def t_extract_empty():
    import asyncio
    from astrbot_plugin_life_memory import extract as ex

    prov = FakeProvider('{"person": {}, "events": []}')
    person, events = asyncio.run(ex.extract(prov, "今天天气还行", ""))
    n = ex.save("selftest:none", "s", person, events)
    assert n == 0 and not store.active_events("selftest:none"), "空结果不该落库"
    return "空结果不落库"


def t_speak_clean():
    from astrbot_plugin_life_memory import speak

    assert speak.clean('她说：“（敲了敲盆）白饭见底了。”') == "（敲了敲盆）白饭见底了。"
    assert "\n" not in speak.clean("第一行\n第二行")
    long_text = speak.clean("这是一句非常非常长的话" * 8)
    assert len(long_text) <= speak.MAX_CHARS, len(long_text)
    return "去掉前缀/引号/换行，超长截断"


def t_speak_service_filter():
    from astrbot_plugin_life_memory import speak

    assert speak.looks_like_customer_service("在吗，有人吗")
    assert speak.looks_like_customer_service("大家今天过得怎么样")
    assert not speak.looks_like_customer_service("（把盆翻过来看了看）空的")
    return "客服腔被识别，自言自语放过"


def t_speak_compose():
    import asyncio
    from astrbot_plugin_life_memory import speak

    ok = FakeProvider("“（把盆翻过来看了看）空的。”")
    text = asyncio.run(speak.compose(ok, "selftest:speak"))
    assert text == "（把盆翻过来看了看）空的。", text

    bad = FakeProvider("在吗，有人吗，需要我帮忙随时说")
    assert asyncio.run(speak.compose(bad, "selftest:speak")) == "", "客服腔没被拦"
    return f"好的一句话：{text}；客服腔被丢"



def t_speak_unasked_question():
    """没人问的问题不许自己答：18:16 那句"周末啊……"就是条件剧本漏出来的。"""
    import asyncio
    from astrbot_plugin_life_memory import dayplan, speak, state

    assert speak.looks_like_answer("周末啊……混过去就行了吧"), "应答腔没认出来"
    assert speak.looks_like_answer("（敲了敲盆）还行吧。"), "带括号动作的应答没认出来"
    assert not speak.looks_like_answer("本鱼的盆又空了"), "自言自语被误判成应答"

    act = state._strip_script("在群里冒个泡，被人问周末过得怎样，糊弄过去")
    assert "被人问" not in act and "冒个泡" in act, f"条件句没剥干净：{act}"
    assert "被人问" in dayplan.CONDITIONAL, "日计划校验里没有条件词表"

    # 这个会话没人说过话（静场），应答腔必须被丢掉
    p = FakeProvider("“周末啊……混过去就行了吧。”")
    assert asyncio.run(speak.compose(p, "selftest:unasked")) == "", "静场里放过了应答腔"
    ok = FakeProvider("“（把盆翻过来看了看）空的。”")
    assert asyncio.run(speak.compose(ok, "selftest:unasked")), "正常的一句被误杀"
    return "静场里应答腔被丢，正常的一句照发"


def t_speak_no_repeat():
    """最近说过的话要进提示词，否则她会连着用同一个动作开头。"""
    from astrbot_plugin_life_memory import speak

    sid = "selftest:repeat"
    store.record_speech(sid, 12, text="（翻了个身）谁半夜还醒着啊。")
    prompt = speak.build_prompt(sid)
    assert "她最近说过" in prompt, prompt
    assert "不要再用同样的开头" in prompt, prompt
    kept = store.recent_speech_texts(sid, 3)
    assert kept and "翻了个身" in kept[0], kept
    return "最近发言进了提示词，并要求换开头"



def t_state_fallback():
    """计划块之间有缝隙时，状态块要给平实兜底，不能留白让她自由发挥。"""
    import datetime as dt

    today = store.today_local()
    dayplan.save_day(today, dayplan.parse_plan(PLAN_FIXTURE))
    with store.connect() as conn:
        conn.execute("DELETE FROM life_block WHERE day=?", (today,))

    now = dt.datetime.now(store.TZ).replace(hour=10, minute=32)
    b = state.build("selftest:fb", now)
    assert "手上没什么事" in b, b
    assert "待着" in b, b

    dayplan.save_day(today, dayplan.parse_plan(PLAN_FIXTURE))   # 复原，后面的用例还要用
    return "空隙时给兜底，不留白"



def t_speak_avoid_repeat_action():
    """上句是括号开头，这句要先要求换写法；模型不听就再要一次。"""
    import asyncio
    from astrbot_plugin_life_memory import speak

    sid = "selftest:action"
    prov = FakeProvider("（拍了拍桌角）嗯。")
    first = asyncio.run(speak.compose(prov, sid))
    assert first, "第一句应当能出来"
    assert prov.calls == 1, prov.calls
    store.record_speech(sid, len(first), text=first)   # 线上由 remember_spoken 记，测试里手动补

    second = asyncio.run(speak.compose(prov, sid))
    assert prov.calls == 3, f"第二句应当重试一次（共 3 次调用），实际 {prov.calls}"
    assert second, "重试两次仍不成时，宁可给句式重复的句子，也不要不说话"
    return "上句括号开头时重问一次，不轻易放弃"



def t_speech_noise():
    """空回复不记；几百字的长回复不算"她说过的话"。"""
    sid = "selftest:noise"
    store.record_speech(sid, 0, text="")                      # 工具调用产生的空回复
    store.record_speech(sid, 400, text="很长的开发说明" * 40)   # 长回复
    store.record_speech(sid, 8, text="（敲了敲盆）空的。")      # 正常短句

    with store.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM speech_log WHERE session=?", (sid,)).fetchone()[0]
    assert n == 1, f"空回复和 400 字长文都该丢掉，实际记了 {n} 条"

    texts = store.recent_speech_texts(sid)
    assert texts == ["（敲了敲盆）空的。"], texts
    return "空回复与超长文本都不记；只有短句进复读检测"



def t_private_gate():
    """私聊主动：好感不够不找；上一条没回不追第二句；回了就恢复。"""
    import datetime as dt
    from astrbot_plugin_life_memory import gate

    uid = "selftest:private"
    now = dt.datetime.now(store.TZ).replace(hour=20, minute=0)
    utc = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    assert not gate.can_speak_private(uid, True, relation=20, now=now)["ok"], "好感不够也找了"
    assert gate.can_speak_private(uid, True, relation=70, now=now)["ok"], "该找却没找"

    pid = store.record_proactive(uid, 0.8, ts=utc)
    r = gate.can_speak_private(uid, True, relation=70, now=now)
    assert not r["ok"] and "还没回" in r["reason"], r

    store.mark_proactive_replied(uid)
    later = now + dt.timedelta(hours=2)
    r2 = gate.can_speak_private(uid, True, relation=70, now=later)
    assert "今天已经找过" in r2["reason"], r2   # 20 小时冷却内不再找他

    much_later = (now + dt.timedelta(days=1)).replace(hour=20)   # 第二天晚上，避开睡眠时段
    assert gate.can_speak_private(uid, True, relation=70, now=much_later)["ok"], "隔天应当可以再找"
    return f"不追第二句；隔天恢复（记录 #{pid}）"


def t_relation():
    """聊一次近一点，但有上限——不能变成谁话多谁最亲的排行榜。"""
    uid = "selftest:relation"
    for _ in range(3):
        store.bump_relation(uid)
    assert store.get_person(uid)["relation"] == 6, store.get_person(uid)["relation"]
    for _ in range(500):
        store.bump_relation(uid)
    assert store.get_person(uid)["relation"] == 800, store.get_person(uid)["relation"]
    assert relation.describe(6) == "初次见面" and relation.describe(70) == "比较喜欢"
    return "每次 +2，封顶 800；档位描述与 self_learning 一致"



def t_no_repeat_topic():
    """提过的事要降权：同一件事反复拿出来说，是最明显的"不像人"。"""
    import datetime as dt
    from astrbot_plugin_life_memory import recall

    now = dt.datetime.now(store.TZ)
    ev = {"id": 999, "importance": 3, "ts": store.now_utc(), "keywords": "考试 复习",
          "kind": "info", "summary": "他说在准备考试"}
    base = recall._score(ev, set(), "准备得怎么样", now, used=set())
    used = recall._score(ev, set(), "准备得怎么样", now, used={999})
    assert abs((base - used) - recall.USED_PENALTY) < 0.001, (base, used)
    return f"提过之后降 {recall.USED_PENALTY} 分（{base:.1f}→{used:.1f}）"



DIARY_OK = ("今天基本没干成什么。早上想把那个没用的东西弄明白，弄到一半就不想动了。"
            "饭倒是吃了两顿，第二顿有点凉。下午有人说了句好笑的，我假装没笑。"
            "晚上有点困，但不想睡，就这样。")
DIARY_CLICHE = DIARY_OK + "真是充实的一天。"


def t_mood():
    """心情要有来处：坏事件、被无视，都该往下压。"""
    from astrbot_plugin_life_memory import mood

    base = mood.score()["score"]
    uid = "selftest:mood"
    for _ in range(2):
        store.add_event(uid, "s", "conflict", "他又跟我拌嘴了", sentiment="负", importance=3)
    low = mood.score()["score"]
    assert low < base, f"两件不顺的事没让心情变差：{base} → {low}"

    sid = "selftest:mood:pro"
    for _ in range(2):
        store.record_proactive(sid, 0.8)
    l2 = mood.score()["score"]
    # 悲喜都有上限：再加"没人理"也不会无限往下掉。心情会差，但不会掉进坑里出不来。
    assert l2 <= low, f"被无视后没下降：{low} → {l2}"
    assert l2 >= 0.0 and low >= 0.0
    return f"基准 {base} → 有冲突 {low} → 又不理我 {l2}（负面有上限，不掉进坑里）"


def t_diary_flow():
    """流水账检测：一段里堆三个时段词，就该判掉。"""
    from astrbot_plugin_life_memory import diary

    log_like = "早上起来喝了杯水，上午看了会儿窗外，下午又睡了觉，晚上把灯关了。"
    assert diary.looks_like_log(log_like), log_like
    ok = "把桌子挪了个位置，发现线不够长，又挪回去了。白饭吃了两顿。"
    assert not diary.looks_like_log(ok), ok
    return "从早写到晚会判掉，只写一件事的不判"


def t_diary_validate():
    from astrbot_plugin_life_memory import diary

    text = diary.validate(DIARY_OK)
    assert len(text) >= diary.MIN_CHARS
    for bad, why in ((DIARY_CLICHE, "套话"), ("太短了", "太短"), ("今天天气不错，出门走了一圈，风很舒服", "没有第一人称")):
        try:
            diary.validate(bad)
        except diary.DiaryError:
            continue
        raise AssertionError(f"{why} 没被拒绝：{bad[:20]}")
    return "套话、太短、非第一人称都被拒"


def t_diary_generate():
    import asyncio
    from astrbot_plugin_life_memory import diary

    prov = FakeProvider(DIARY_OK)
    got = asyncio.run(diary.generate(prov, "2026-10-25"))
    assert got["text"] == diary.validate(DIARY_OK)
    saved = store.load_diary("2026-10-25")
    assert saved and saved["text"] == got["text"], saved
    again = asyncio.run(diary.ensure_yesterday(prov, __import__("datetime").datetime(2026, 10, 26, 9, 0)))
    assert again is None, "已经写过就不该再写"
    return f"{len(got['text'])} 字落库；重复调用不重写"



def t_rollup():
    """周记只拼接、不推断；原文一条不删；检索时以粗颗粒带出来。"""
    import datetime as dt
    from astrbot_plugin_life_memory import recall, rollup

    uid = "selftest:rollup"
    today = dt.datetime.now(store.TZ).date()
    last_week = today - dt.timedelta(days=7)
    monday = last_week - dt.timedelta(days=last_week.weekday())
    for i, summary in enumerate(["他说在准备搬家，房东反复", "他抱怨食堂涨价了", "他提了一句想学吉他"]):
        ts = dt.datetime.combine(monday + dt.timedelta(days=i), dt.time(12, 0))
        store.add_event(uid, "s", "info", summary, keywords="搬家 吉他", importance=4,
                        ts=ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    row = rollup.build_week(uid, last_week)
    assert row and "搬家" in row["summary"], row
    assert "；" in row["summary"], "应当拼接多条"
    n_before = len(store.active_events(uid))

    store.save_rollup(uid, "week", row["period"], row["summary"])
    made = rollup.ensure_last_week(today)
    same = [m for m in made if m["uid"] == uid]
    assert not same, "同一周不该重复生成"

    assert len(store.active_events(uid)) == n_before, "压缩不应删除原文"
    hit = recall.retrieve(uid, "他搬家那事怎么样了", k=2)
    assert "那阵子" in hit["text"], hit["text"]
    return f"周记 {len(row['summary'])} 字，原文 {n_before} 条未删，检索带出粗颗粒"



def t_moments():
    """说说=日记本身，不另写一份；挑不到图就不配图。"""
    import asyncio
    import os
    from astrbot_plugin_life_memory import moments


    prov = FakeProvider(DIARY_OK)
    got = asyncio.run(moments.ensure_from_diary(prov, None, "2026-10-24", with_image=False))
    assert got["text"] == DIARY_OK, "说说正文应当就是日记正文"
    assert got["image_path"] == "", "没有 context 就不该配图"
    row = store.get_moment(got["id"])
    assert row and row["text"] == DIARY_OK
    return f"#{got['id']} 直接复用日记；无素材时不配图"


def t_work():
    """三类来源说三种话；失败的活要说得出来，但不许编原因。"""
    from astrbot_plugin_life_memory import state, work

    work.log("重排了今天的安排", "manual")
    assert "分身被你叫去" in work.brief(), work.brief()

    work.log("补了昨天的日记", "scheduled")
    assert "分身把该做的做了" in work.brief(), work.brief()

    work.log("写了条说说稿", "scheduled", status="fail", note="校验没过")
    assert "没成" in work.brief(), work.brief()

    # 日记是本体写的，不能挂到分身头上
    work.log("写了昨天的日记", "scheduled", actor="self")
    assert "你刚才写了昨天的日记" in work.brief(), work.brief()

    sid = "selftest:work"
    b = state.build(sid, user_text="分身今天干了什么")
    assert "你刚才写了昨天的日记" in b or "日记" in b, b
    return f"分身/本体语气分开；状态块带出「{work.brief()[:20]}…」"



def t_push_once():
    """晚报一天只发一条：重复 mark 要返回 False。"""
    day = "2026-10-26"
    assert store.mark_push(day, "evening") is True
    assert store.mark_push(day, "evening") is False, "重复占位没被挡住"
    assert store.pushed(day, "evening") is True
    return "同一天第二次占位返回 False"


def t_voice():
    """口吻接口：模板句要拦住，好句子要留下，长度超了要截。"""
    import asyncio
    from astrbot_plugin_life_memory import voice

    ok = FakeProvider("“（盆还扣在头上）今天群里那条说说我转到这儿了，爱看不看。”")
    text = asyncio.run(voice.say(ok, "forward", {"群名": "老地方"}))
    assert text and len(text) <= voice.SPECS["forward"]["max"], text

    templated = FakeProvider("祝你生日快乐，天天开心，万事如意")
    assert asyncio.run(voice.say(templated, "birthday_greet", {"昵称": "老张"})) == "", "祝福模板没被拦"

    service = FakeProvider("很高兴为您服务，需要我帮忙随时找我")
    assert asyncio.run(voice.say(service, "birthday_ask")) == "", "客服腔没被拦"

    unknown = asyncio.run(voice.say(ok, "no_such_kind"))
    assert unknown == "", "未知类型应当返回空"
    return f"好句子留下（{len(text)} 字）；模板、客服腔、未知类型都返回空"



def t_page_html():
    """页面 HTML 的硬伤检查。

    2026-10-26 03:57 的教训：JS 注释里写了一个 </body>，被服务端"把 </body> 替换成
    桥接脚本"的注入逻辑命中，脚本从中间被切断，后面整段 JS 变成页面上的文字。
    这类错误不报错、不抛异常，只是"页面长得像乱码"，所以必须让自检盯着。
    """
    import os

    page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "app", "index.html")
    html = open(page, encoding="utf-8").read()

    assert html.count("</body>") == 1, f"</body> 出现 {html.count('</body>')} 次，注入会插错位置"
    assert html.count("</script>") == 1, f"</script> 出现 {html.count('</script>')} 次"
    js = html.split("<script>")[1].split("</script>")[0]
    assert "</body>" not in js, "脚本里出现了 </body>，会被注入逻辑切断"
    assert "</script>" not in js, "脚本里出现了 </script>"
    return f"body/script 标签各 1 个，脚本内无越界标签（{len(html)} 字节）"



def t_quiet_ranges():
    """静默区间要真的读 ranges——2026-10-26 那个"读不到字段、永远走兜底"的坑。"""
    import json as _json
    import os
    import tempfile

    from astrbot_plugin_life_memory import paths, state

    orig = paths.quiet_hours_config
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        _json.dump({"enabled": True, "ranges": "22:00-06:30"}, open(tmp, "w", encoding="utf-8"))
        paths.quiet_hours_config = lambda: tmp
        state._QUIET_CACHE.update({"mtime": -1.0, "cfg": {}})
        assert state.sleep_window() == ("22:00", "06:30"), state.sleep_window()
        assert state.awake_span() == ("06:30", "22:00")

        _json.dump({"enabled": False, "ranges": "22:00-06:30"}, open(tmp, "w", encoding="utf-8"))
        os.utime(tmp, (0, 0))
        state._QUIET_CACHE.update({"mtime": -1.0, "cfg": {}})
        assert state.quiet_disabled() is True
    finally:
        paths.quiet_hours_config = orig
        state._QUIET_CACHE.update({"mtime": -1.0, "cfg": {}})
        os.unlink(tmp)
    return "22:00-06:30 能读到；enabled=false 时不判睡"


def t_space_draft_schema():
    """写入发布插件草稿的列必须与它的表结构一致（含 forward_target）。"""
    import json as _json
    import os
    import sqlite3
    import tempfile

    from astrbot_plugin_life_memory import moments, paths

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        con = sqlite3.connect(tmp)
        con.execute("""CREATE TABLE drafts(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT,
            source TEXT, status TEXT, tid TEXT, error TEXT, created_at INTEGER,
            published_at INTEGER, image_path TEXT, subject_id TEXT, subject_name TEXT,
            forwarded_at INTEGER, forward_target TEXT, forward_note TEXT)""")
        con.execute("""CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,
            draft_id INTEGER, detail TEXT, created_at INTEGER)""")
        con.commit()
        con.close()

        orig = paths.space_db
        paths.space_db = lambda: tmp
        try:
            did = moments.to_space_draft("测试正文", "/tmp/x.png", "转发语", "123456789")
        finally:
            paths.space_db = orig
        assert did, "没能写入草稿"

        con = sqlite3.connect(tmp)
        row = con.execute("SELECT text, image_path, forward_note, forward_target FROM drafts WHERE id=?",
                          (did,)).fetchone()
        con.close()
        assert row == ("测试正文", "/tmp/x.png", "转发语", "123456789"), row
    finally:
        os.unlink(tmp)
    return f"草稿 #{did} 四列都对（含转发目标）"



def t_config_keys():
    """配置项缺一个就会静默失效（2026-10-26 04:14 真踩过：覆盖写时丢了会话白名单）。"""
    import json as _json
    import os

    from astrbot_plugin_life_memory import paths

    path = paths.config_path()
    if not os.path.exists(path):
        return "没有配置文件（跳过）"
    cfg = _json.load(open(path, encoding="utf-8-sig"))
    need = ["extract_enabled", "extract_sessions", "extract_hourly_limit",
            "proactive_enabled", "proactive_sessions", "proactive_private_enabled",
            "moment_auto", "moment_forward_target",
            "evening_report_enabled", "evening_report_time", "evening_report_target",
            "diary_hour", "extract_private_all"]
    missing = [k for k in need if k not in cfg]
    assert not missing, f"配置缺项：{missing}"
    assert cfg["extract_sessions"], "抽取白名单为空，等于不抽"
    return f"{len(need)} 项齐全，白名单 {len(cfg['extract_sessions'])} 个会话"



def t_main_imports():
    """main.py 里用到的本插件模块，必须真的被 import。

    2026-10-26 04:41 的教训：加了 /测配图，用了 scenes.SCENES，
    却忘了在 import 列表里加 scenes —— 命令一敲就 NameError。
    """
    import ast
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    tree = ast.parse(open(os.path.join(here, "main.py"), encoding="utf-8").read())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is None:
            imported.update(a.name for a in node.names)

    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    # 这些是标准库或局部变量前缀，不算
    known = {"astrbot", "self", "datetime", "os", "re", "json", "asyncio", "logging",
             "sqlite3", "star", "event", "request", "jsonify", "Plain", "MessageChain",
             "SimpleNamespace", "context", "config", "LOG"}
    missing = sorted(used - imported - known)
    # 局部变量（如 b.、r.、m.）不在 imported，但也不会是小写模块名集合里的真实模块
    modules = {"dayplan", "diary", "extract", "gate", "mood", "moments", "proactivity",
               "recall", "paths", "relation", "rollup", "scenes", "speak", "state",
               "store", "voice", "work"}
    missing = [m for m in missing if m in modules]
    assert not missing, f"用了但没 import：{missing}"
    return f"17 个本插件模块引用全都有 import"




def t_scene_variants():
    """配图不能天天同一张：同一时段随日期轮换，同一天稳定。"""
    import datetime as dt
    from astrbot_plugin_life_memory import scenes

    a = scenes.build_prompt(dt.datetime(2026, 10, 26, 19, 0), day="2026-10-26")[1]
    b = scenes.build_prompt(dt.datetime(2026, 10, 27, 19, 0), day="2026-10-27")[1]
    a2 = scenes.build_prompt(dt.datetime(2026, 10, 26, 19, 0), day="2026-10-26")[1]
    assert a == a2, "同一天同一时段应当稳定"
    assert a != b, "跨天应当换一张，不能每天一样"

    variants = {scenes.build_prompt(dt.datetime(2026, 10, 26, 19, 0), force_key="night",
                                    variant=i)[1] for i in range(3)}
    assert len(variants) == 3, f"night 三个变体应当互不相同，实际 {len(variants)}"
    return "同天稳定、跨天变化、每时段三变体"



def t_group_sessions_a_real_group():
    """配置里的"群"必须真的是群。

    2026-10-26 04:54 主人指出：3680089564 是私聊，我却当群填进了主动名单——
    那会让她对着一个私聊像对群一样插话。类型记在 message_log.is_group 里，别再靠猜。
    """
    import json as _json
    import os

    from astrbot_plugin_life_memory import paths

    path = paths.config_path()
    if not os.path.exists(path):
        return "没有配置文件（跳过）"
    cfg = _json.load(open(path, encoding="utf-8-sig"))
    groups = [str(x) for x in (cfg.get("proactive_sessions") or [])]
    wrong, unknown = [], []
    for g in groups:
        kind = store.session_is_group(g)
        if kind is None:
            unknown.append(g)
        elif kind == 0:
            wrong.append(g)
    assert not wrong, f"这些被当成群，但实际是私聊：{wrong}"
    return f"{len(groups)} 个群会话类型正确" + (f"（{len(unknown)} 个暂无记录）" if unknown else "")



def t_moment_delete():
    """删除按钮必须真删：删完查不到，重复删返回 False。"""
    mid = store.save_moment("2000-01-02", "删除测试稿", "", "")
    assert store.get_moment(mid), "没建出来"
    assert store.delete_moment(mid) is True, "第一次删除应当返回 True"
    assert store.get_moment(mid) is None, "删完还能查到，等于没删"
    assert store.delete_moment(mid) is False, "重复删应当返回 False"
    return f"#{mid} 删得掉、查不到、重复删返回 False"



def t_memory_search():
    """记忆多了要能筛能翻：关键词、情绪、重要度、分页总数都要对。"""
    uid = "selftest:search"
    store.add_event(uid, "s", "info", "他说要搬家，房东变卦", keywords="搬家 房东", sentiment="负", importance=4)
    store.add_event(uid, "s", "info", "他夸了句今天的饭不错", keywords="白饭 夸", sentiment="正", importance=1)
    store.add_event(uid, "s", "promise", "他答应周末还书", keywords="书 周末", sentiment="中", importance=3)

    rows, total = store.search_events(q="搬家", uid=uid)
    assert total == 1 and "搬家" in rows[0]["summary"], (total, rows)

    rows, total = store.search_events(sentiment="正", uid=uid)
    assert total == 1 and rows[0]["sentiment"] == "正", (total, rows)

    rows, total = store.search_events(min_importance=3, uid=uid)
    assert total == 2, total

    page1, total = store.search_events(limit=2, offset=0, uid=uid)
    page2, _ = store.search_events(limit=2, offset=2, uid=uid)
    assert len(page1) == 2 and len(page2) == 1, (len(page1), len(page2))
    assert total == 3, total
    return f"关键词/情绪/重要度各筛一次，分页 2+1=3 条"



def t_master_marker():
    """主人说话时，状态块要点明身份——她的设定里"主人"是个具体的人，
    但每次对话她只看到昵称，不点明就只能按普通用户对待。"""
    import datetime as dt
    from astrbot_plugin_life_memory import state

    now = dt.datetime.now(store.TZ).replace(hour=10, minute=0)
    a = state.build("selftest:who", now, is_master=True)
    b = state.build("selftest:who", now, is_master=False)
    assert "就是主人本人" in a, a
    assert "就是主人本人" not in b, b
    return "主人说话时状态块会点明，别人不会"



def t_event_delete():
    """记忆删除要真删：删完检索不到；重复删返回 False。"""
    uid = "selftest:evdel"
    eid = store.add_event(uid, "s", "info", "这条要被删掉", keywords="删测试")
    assert any(e["id"] == eid for e in store.active_events(uid)), "没建出来"
    assert store.delete_event(eid) is True
    assert not any(e["id"] == eid for e in store.active_events(uid)), "删完还在"
    assert store.delete_event(eid) is False, "重复删应当 False"
    return f"#{eid} 删得掉、检索不到、重复删 False"


def t_diary_rewrite():
    """早于「发布前一小时」写的日记不算数，要重写。"""
    import datetime
    from astrbot_plugin_life_memory import diary, store

    cut = datetime.datetime(2026, 10, 26, 22, 0, tzinfo=store.TZ)   # 发布 23:00 → 检查点 22:00
    # 反正都要过一遍本地时区换算
    assert diary.too_early({"created_at": "2026-10-25 16:11:32"}, cut) is True, "凌晨写的没被判掉"
    assert diary.too_early({"created_at": "2026-10-26 07:00:00"}, cut) is True, "下午写的（早于检查点）没被判掉"
    assert diary.too_early({"created_at": "2026-10-26 14:30:00"}, cut) is False, "过了检查点写的被判掉了"
    assert diary.too_early(None, cut) is False, "没有日记时不该说'不算'"
    # 没给 cutoff 时退回旧口径，只为兼容
    assert diary.too_early({"created_at": "2026-10-26 16:11:32"}) is True
    return "早于检查点(发布前1h)的不算，过了的不重写"


def t_alias_merge():
    """并库：网页端和 QQ 号是同一个人的两种身份，读写两头都不许分叉。"""
    store.init_db()
    qq, web = "3000000001", "selftest_webchat"
    store.upsert_person(qq, names={qq: "主人"}, impression="忙完会来问一句")
    store.upsert_person(web, names={"webchat": "主人"}, impression="话少，但每次都在")
    store.bump_relation(qq, 800)
    store.bump_relation(web, 40)
    store.alias_add(web, qq, "自检")

    assert store.canon(web) == qq, f"别名没换成正主：{store.canon(web)}"
    assert store.aliases_of(qq) == [qq, web], f"身份列表不对：{store.aliases_of(qq)}"

    p = store.get_person(web)
    assert p and p["uid"] == qq, "从网页端读不到正主"
    assert "webchat" in p["names"] and qq in p["names"], f"称呼没合并：{p['names']}"
    assert p["relation"] == 800, f"好感度没取大的：{p['relation']}"
    assert len(p["impression"]) >= len("话少，但每次都在"), "印象没合成长的那条"

    eid = store.add_event(web, "webchat:selftest", "info", "他在网页端也说了一句", importance=3)
    assert any(e["id"] == eid for e in store.active_events(qq)), "网页端写的事件，正主那边没长出来"
    with store.connect() as conn:
        stored = conn.execute("SELECT uid FROM event WHERE id=?", (eid,)).fetchone()[0]
    assert stored == qq, f"还是按旧身份落库：{stored}"

    twins = [r["uid"] for r in store.relations(min_relation=1, limit=20)].count(qq)
    assert twins <= 1, f"relations 里出现了 {twins} 次同一个人"
    assert store.uids_with_events().count(qq) == 1, "uids_with_events 没去重"
    return f"{web} → {qq}；称呼合并 2 个，好感度取 800，写入不新开身份"


def t_alias_rollback():
    """别名表没了也不许炸：先删表后回滚代码是安全顺序。"""
    with store.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS person_alias")
    store._ALIAS = None
    assert store.canon("whatever") == "whatever", "没有别名表时 canon 应当原样返回"
    assert store.aliases_of("whatever") == ["whatever"], "没有别名表时身份列表应当只有自己"
    return "删掉 person_alias 后 canon 原样返回，不抛错"


for name, fn in [
    ("日记半成品重写", t_diary_rewrite),
    ("别名合并", t_alias_merge),
    ("不回答没人问的问题", t_speak_unasked_question),
    ("别名表缺失不炸", t_alias_rollback),
    ("建表与幂等", t_schema),
    ("脏数据不炸", t_should_stop_on_dirty_data),
    ("检索预算", t_retrieve_budget),
    ("场景区分度", t_scene_sensitivity),
    ("无档案腔", t_no_archive_tone),
    ("衰减规则", t_decay),
    ("睡眠窗口", t_sleep_window),
    ("状态块", t_state_block),
    ("空会话", t_empty_session),
    ("日计划解析", t_plan_parse),
    ("坏计划被拒", t_plan_reject),
    ("ensure_day 幂等", t_ensure_idempotent),
    ("生成失败不落库", t_plan_failure_silent),
    ("状态块带当前活动", t_state_has_block),
    ("渲染与强制重排", t_render_and_force),
    ("睡着被叫醒", t_disturbed_awake),
    ("被叫次数渐变", t_wake_gradient),
    ("主动意愿", t_proactivity),
    ("闸门五道", t_gate),
    ("该跳过的话", t_skip_rules),
    ("抽取校验", t_extract_validate),
    ("抽取入库并想起", t_extract_to_memory),
    ("空抽取不落库", t_extract_empty),
    ("主动措辞清洗", t_speak_clean),
    ("客服腔过滤", t_speak_service_filter),
    ("主动措辞生成", t_speak_compose),
    ("防止复读开头", t_speak_no_repeat),
    ("空隙兜底", t_state_fallback),
    ("避免连续括号开头", t_speak_avoid_repeat_action),
    ("发言噪音过滤", t_speech_noise),
    ("私聊主动规则", t_private_gate),
    ("好感度累加", t_relation),
    ("别絮叨降权", t_no_repeat_topic),
    ("心情有来处", t_mood),
    ("日记校验", t_diary_validate),
    ("流水账检测", t_diary_flow),
    ("日记生成与幂等", t_diary_generate),
    ("周记压缩", t_rollup),
    ("说说稿", t_moments),
    ("工作量三档", t_work),
    ("口吻接口", t_voice),
    ("晚报一天一条", t_push_once),
    ("页面标签完整", t_page_html),
    ("静默区间解析", t_quiet_ranges),
    ("发布草稿表结构", t_space_draft_schema),
    ("配置项齐全", t_config_keys),
    ("main 的模块导入", t_main_imports),
    ("配图变体轮换", t_scene_variants),
    ("群会话类型", t_group_sessions_a_real_group),
    ("删除真能删", t_moment_delete),
    ("记忆可筛选分页", t_memory_search),
    ("主人身份标记", t_master_marker),
    ("记忆删除", t_event_delete),
]:
    check(name, fn)

print("\n===== 自检结果 =====")
for ok, name, detail in RESULTS:
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
failed = [r for r in RESULTS if not r[0]]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
sys.exit(1 if failed else 0)
