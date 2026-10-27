"""life_memory 存储层 —— 库里所有写入都从这里走（单一写入方）。

时间约定（数据模型.md §1）：
- 时间列 TEXT，存 UTC "YYYY-MM-DD HH:MM:SS"，不带时区后缀
- 日期列 TEXT，存本地 "YYYY-MM-DD"（"今天"是人的概念）
- 不在 SQL 里做本地日期运算，读写各转一次
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def set_timezone(name: str) -> None:
    """按配置换时区，默认仍是 Asia/Shanghai（原来写死，分享出去要能改）。

    别的模块都是现取 `store.TZ`（不是 from store import TZ），所以重绑这个
    全局变量就等于全场生效；名字不认就退回默认，别让插件起不来。
    """
    global TZ
    for candidate in (str(name or "").strip(), "Asia/Shanghai"):
        if not candidate:
            continue
        try:
            TZ = ZoneInfo(candidate)
            return
        except Exception:  # noqa: BLE001
            continue


from . import paths

DEFAULT_DB = ""   # 实际路径由 paths.db_path() 决定，见下方 db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS life_day (
    day         TEXT PRIMARY KEY,
    plan        TEXT NOT NULL,
    plan_note   TEXT DEFAULT '',
    mood        TEXT DEFAULT '',
    energy      INTEGER DEFAULT 60,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS life_block (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    activity    TEXT NOT NULL,
    place       TEXT DEFAULT '',
    attention   TEXT DEFAULT 'idle',
    energy      INTEGER DEFAULT 60,
    origin      TEXT DEFAULT 'plan',
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_block_day ON life_block(day, start_at);

CREATE TABLE IF NOT EXISTS scene_cache (
    block_id    INTEGER NOT NULL,
    session     TEXT NOT NULL,
    text        TEXT NOT NULL,
    tokens      INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (block_id, session)
);

CREATE TABLE IF NOT EXISTS person (
    uid           TEXT PRIMARY KEY,
    names         TEXT DEFAULT '',
    first_seen    TEXT,
    last_seen     TEXT,
    impression    TEXT DEFAULT '',
    notes         TEXT DEFAULT '',
    relation      INTEGER DEFAULT 0,
    updated_at    TEXT
);

-- 一个人可以有多个身份（QQ 号 / 网页端）。这张表只记"谁是正主"，
-- 不搬任何既有行；删掉它就等于取消合并。
CREATE TABLE IF NOT EXISTS person_alias (
    uid         TEXT PRIMARY KEY,
    canonical   TEXT NOT NULL,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    session     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    keywords    TEXT DEFAULT '',
    sentiment   TEXT DEFAULT '',
    importance  INTEGER DEFAULT 1,
    decay_at    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_uid ON event(uid, ts DESC);
CREATE INDEX IF NOT EXISTS idx_event_ts ON event(ts DESC);   -- 全局按时间翻页要用

CREATE TABLE IF NOT EXISTS memory_rollup (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    level       TEXT NOT NULL,
    period      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(uid, level, period)
);

CREATE TABLE IF NOT EXISTS memory_use (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    event_ids   TEXT DEFAULT '',
    session     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    reply_len   INTEGER DEFAULT 0
);

-- 2026-10-26 增设：M2 状态块需要"上次说话多久前 / 今天在这个群说了几句"。
-- memory_use 记的是"用了哪些记忆"，语义不同，不能复用。
CREATE TABLE IF NOT EXISTS speech_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    chars       INTEGER DEFAULT 0,
    kind        TEXT DEFAULT 'reply',
    text        TEXT DEFAULT '',         -- 原文，用来避免连续重复同一个开头
    uid         TEXT DEFAULT ''          -- 2026-10-28 增设：谁在听她说，网页端/群聊要分开
);
CREATE INDEX IF NOT EXISTS idx_speech_session ON speech_log(session, ts DESC);

-- 2026-10-26 增设：睡着时被叫的记录。用来做"渐变"——第 1 次和说明第 5 次的反应不一样，
-- 跟人被吵醒一样。记的是次数，不是文案。
CREATE TABLE IF NOT EXISTS wake_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    ts          TEXT NOT NULL
);
-- 她的日记：一天一段，自己回头看的东西（M5）
CREATE TABLE IF NOT EXISTS diary (
    day         TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    mood        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 她动过的活：只记发生过什么，不记怎么做的（不入侵别的插件）
CREATE TABLE IF NOT EXISTS work_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT NOT NULL,          -- 人话，一句话
    trigger     TEXT DEFAULT 'manual',  -- manual / scheduled / passive
    actor       TEXT DEFAULT 'clone',   -- clone=分身 / self=她本体
    source      TEXT DEFAULT 'self',
    status      TEXT DEFAULT 'ok',      -- ok / fail
    note        TEXT DEFAULT '',
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_ts ON work_log(ts DESC);

-- 定时推送去重（晚报一天一条，重启也不重复）
CREATE TABLE IF NOT EXISTS push_log (
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    UNIQUE(day, kind)
);

-- 说说稿：对外的动态，独立于日记（日记是自己回看的）
CREATE TABLE IF NOT EXISTS moment_draft (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    text        TEXT NOT NULL,
    mood        TEXT DEFAULT '',
    status      TEXT DEFAULT 'draft',   -- draft / used / dropped
    image_path  TEXT DEFAULT '',        -- 配图（从她收到的图里挑，没有就空）
    created_at  TEXT NOT NULL
);

-- 2026-10-26 增设：群里的来话记录。只为了回答一个问题——这个群此刻是活的吗。
-- 写库很便宜但不必每条都写，按 session 节流一分钟一次。
CREATE TABLE IF NOT EXISTS message_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    uid         TEXT DEFAULT '',
    is_group    INTEGER DEFAULT 0,   -- 1=群 0=私聊。别再靠 id 长度猜（2026-10-26 踩过）
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON message_log(session, ts DESC);

-- 主动发言记录：冷却、退避都从这里算
CREATE TABLE IF NOT EXISTS proactive_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    ts          TEXT NOT NULL,
    willingness REAL DEFAULT 0,
    replied     INTEGER DEFAULT 0,
    text        TEXT DEFAULT '',        -- 2026-10-28：她主动说的原话
    replied_uid TEXT DEFAULT '',        -- 谁接的
    replied_at  TEXT DEFAULT ''         -- 什么时候接的
);
CREATE INDEX IF NOT EXISTS idx_proactive_session ON proactive_log(session, ts DESC);

"""

# 衰减周期（数据模型.md §3.3）：到期不是删除，是检索时不再主动提
DECAY_DAYS = {1: 7, 2: 30, 3: 90, 4: None, 5: None}


def db_path() -> str:
    """数据库路径。环境变量优先（离线自检用），否则走 paths 里的运行时目录。"""
    return paths.db_path()


def prompt_file() -> str:
    """日计划提示词的可编辑副本。存在就优先用它，方便不重载插件直接调提示词。"""
    return paths.prompt_file()


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today_local() -> str:
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def to_local(ts_utc: str) -> datetime.datetime:
    """UTC 字符串 → 带时区的本地时间。解析失败时退回当前时间，别让一条脏数据拖垮检索。"""
    try:
        naive = datetime.datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.datetime.now(TZ)
    return naive.replace(tzinfo=datetime.timezone.utc).astimezone(TZ)


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    """幂等建表。多次调用无副作用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 老库的 speech_log 没有 uid 列：补一列，失败就忽略（只影响归属统计）
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(speech_log)")}
            if "uid" not in cols:
                conn.execute("ALTER TABLE speech_log ADD COLUMN uid TEXT DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        try:
            pcols = {r[1] for r in conn.execute("PRAGMA table_info(proactive_log)")}
            for col, decl in (("text", "TEXT DEFAULT ''"),
                              ("replied_uid", "TEXT DEFAULT ''"),
                              ("replied_at", "TEXT DEFAULT ''")):
                if col not in pcols:
                    conn.execute(f"ALTER TABLE proactive_log ADD COLUMN {col} {decl}")
        except Exception:  # noqa: BLE001
            pass


# ---------- 别名：一个人多个身份 ----------

_ALIAS: dict[str, str] | None = None


def _alias_map(refresh: bool = False) -> dict[str, str]:
    """别名 → 正主。表不存在时返回空字典、不抛错，方便先删表再回滚代码。"""
    global _ALIAS
    if _ALIAS is not None and not refresh:
        return _ALIAS
    m: dict[str, str] = {}
    try:
        with connect() as conn:
            for row in conn.execute("SELECT uid, canonical FROM person_alias"):
                a, c = str(row[0] or ""), str(row[1] or "")
                if a and c and a != c:
                    m[a] = c
    except Exception:  # noqa: BLE001
        m = {}
    _ALIAS = m
    return m


def canon(uid: str) -> str:
    """别名换成正主。最多跟 8 跳，成环就停在原地，不递归死。"""
    u = str(uid or "").strip()
    if not u:
        return u
    m = _alias_map()
    seen = {u}
    for _ in range(8):
        nxt = m.get(u)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        u = nxt
    return u


def aliases_of(uid: str) -> list[str]:
    """一个正主名下的全部身份，正主自己排在第一位。"""
    u = canon(uid)
    return [u] + [a for a, c in _alias_map().items() if canon(c) == u and a != u]


def alias_add(uid: str, canonical: str, note: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO person_alias (uid, canonical, note, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(uid) DO UPDATE SET canonical=excluded.canonical, note=excluded.note",
            (str(uid), str(canonical), note, now_utc()),
        )
    _alias_map(refresh=True)


def alias_list() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM person_alias ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


# ---------- person ----------

def upsert_person(uid: str, names: dict | None = None, impression: str | None = None,
                  notes: str | None = None) -> None:
    uid = canon(uid)
    # 空 uid / "0" / "-1" 是平台给的匿名占位，不是人。
    # 库里那条 uid='0' 的脏人物就是早期这么进来的（2026-10-28 补的拦截）。
    if not uid or uid in ("0", "-1"):
        return
    ts = now_utc()
    with connect() as conn:
        row = conn.execute("SELECT uid FROM person WHERE uid=?", (uid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO person (uid, names, first_seen, last_seen, impression, notes, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (uid, json.dumps(names or {}, ensure_ascii=False), ts, ts,
                 impression or "", notes or "", ts),
            )
        else:
            sets, vals = ["last_seen=?", "updated_at=?"], [ts, ts]
            if names is not None:
                sets.append("names=?")
                vals.append(json.dumps(names, ensure_ascii=False))
            if impression is not None:
                sets.append("impression=?")
                vals.append(impression)
            if notes is not None:
                sets.append("notes=?")
                vals.append(notes)
            vals.append(uid)
            conn.execute(f"UPDATE person SET {', '.join(sets)} WHERE uid=?", vals)


def get_person(uid: str) -> dict | None:
    """正主视图：把别名名下的称呼、印象、好感度合成一条。

    两个「主人」（QQ 号 / 网页端）在库里是两行，这里读出来是一个人。
    物理行一行都没动——合并只发生在读的时候。
    """
    ids = aliases_of(uid)
    marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM person WHERE uid IN ({marks})", ids).fetchall()]
    if not rows:
        return None
    rows.sort(key=lambda r: 0 if r.get("uid") == ids[0] else 1)
    base = rows[0]

    names: dict = {}
    for r in rows:
        try:
            names.update(json.loads(r.get("names") or "{}"))
        except Exception:  # noqa: BLE001
            pass
    if names:
        base["names"] = json.dumps(names, ensure_ascii=False)

    best = max(rows, key=lambda r: len(r.get("impression") or ""))
    base["impression"] = best.get("impression") or ""
    base["relation"] = max(int(r.get("relation") or 0) for r in rows)
    firsts = [r["first_seen"] for r in rows if r.get("first_seen")]
    lasts = [r["last_seen"] for r in rows if r.get("last_seen")]
    base["first_seen"] = min(firsts) if firsts else base.get("first_seen")
    base["last_seen"] = max(lasts) if lasts else base.get("last_seen")
    base["uids"] = ids
    return base


# ---------- event ----------

def decay_at_for(importance: int) -> str | None:
    """按重要度算到期时间；4-5 级永久保留。"""
    days = DECAY_DAYS.get(max(1, min(5, int(importance))), 30)
    if days is None:
        return None
    expire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
    return expire.strftime("%Y-%m-%d %H:%M:%S")


def add_event(uid: str, session: str, kind: str, summary: str, keywords: str = "",
              sentiment: str = "", importance: int = 1, ts: str | None = None) -> int:
    uid = canon(uid)
    ts = ts or now_utc()
    importance = max(1, min(5, int(importance)))
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO event (uid, session, ts, kind, summary, keywords, sentiment,"
            " importance, decay_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, session, ts, kind, summary, keywords, sentiment,
             importance, decay_at_for(importance), ts),
        )
        return int(cur.lastrowid)


def active_events(uid: str, limit: int = 50) -> list[dict]:
    """未过期事件，按时间新近排序。"""
    uid = canon(uid)
    now = now_utc()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM event WHERE uid=? AND (decay_at IS NULL OR decay_at > ?)"
            " ORDER BY ts DESC LIMIT ?",
            (uid, now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def log_use(uid: str, session: str, event_ids: list[int], reply_len: int = 0) -> None:
    """记一条使用日志，用来验证"记忆有没有真的改变回复"。"""
    uid = canon(uid)
    with connect() as conn:
        conn.execute(
            "INSERT INTO memory_use (uid, event_ids, session, ts, reply_len) VALUES (?,?,?,?,?)",
            (uid, ",".join(str(i) for i in event_ids), session, now_utc(), reply_len),
        )


# ---------- speech_log（M2 状态块用） ----------

def record_speech(session: str, chars: int = 0, kind: str = "reply", text: str = "", uid: str = "") -> None:
    """记一次发言。

    两条不记：空文本（工具调用会产生）、超长文本（几百字的说明不是"她说话"，
    记进去会污染"最近说过"，还会让复读检测拿一段技术文档当她的口癖）。
    """
    if not (chars or text):
        return
    if len(text or "") > 200:
        return
    # 开发时的思考痕迹不该进"她说过的话"（2026-10-26 主人发现日志里有）
    if any(m in (text or "") for m in ("🤔", "思考:", "思考：")):
        return
    # 2026-10-28：网页端现在是真的在跟她聊，不再一律跳过。
    # 只有面板的联调会话（session 带 panel/trace 字样）仍然排除。
    if any(tag in str(session) for tag in ("panel", "trace")):
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO speech_log (session, ts, chars, kind, text, uid) VALUES (?,?,?,?,?,?)",
            (session, now_utc(), int(chars), kind, text or "", canon(uid)),
        )


def recent_speech_texts(session: str, limit: int = 3, max_chars: int = 120) -> list[str]:
    """最近说过的短句。长回复（比如开发说明）不算"她的话"，不能进复读检测。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT text FROM speech_log WHERE session=? AND text<>'' AND length(text)<=?"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (session, max_chars, limit),
        ).fetchall()
    return [r[0] for r in rows]


def last_speech(session: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM speech_log WHERE session=? ORDER BY ts DESC LIMIT 1",
            (session,),
        ).fetchone()
    return dict(row) if row else None


def today_speech_count(session: str, day_local: str) -> int:
    """按本地日期算"今天说了几句"。UTC 边界得自己算，数据模型 §1 不允许在 SQL 里转。"""
    start_utc = _local_day_start_utc(day_local)
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM speech_log WHERE session=? AND ts>=?",
            (session, start_utc),
        ).fetchone()
    return int(row[0] or 0)


def _local_day_start_utc(day_local: str) -> str:
    dt = datetime.datetime.strptime(day_local, "%Y-%m-%d").replace(tzinfo=TZ)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- life_block ----------

def current_block(day: str, hhmm: str) -> dict | None:
    """取当前活动块。查不到返回 None——那时她只有"醒着/睡着"，没有细节。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM life_block WHERE day=? AND start_at<=? AND end_at>?"
            " ORDER BY start_at DESC LIMIT 1",
            (day, hhmm, hhmm),
        ).fetchone()
    return dict(row) if row else None


def blocks_for(day: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM life_block WHERE day=? ORDER BY start_at", (day,)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- wake_log（睡着被叫的次数，做渐变用） ----------

def record_wake(session: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO wake_log (session, ts) VALUES (?,?)",
                     (session, now_utc()))


def wakes_since(session: str, since_utc: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM wake_log WHERE session=? AND ts>=? ORDER BY ts",
            (session, since_utc),
        ).fetchall()
    return [dict(r) for r in rows]



# ---------- message_log / proactive_log（闸门用） ----------

def record_message(session: str, uid: str = "", ts: str | None = None,
                   is_group: bool = False) -> None:
    """记一条群里的来话。调用方负责节流，这里不做判断。

    `ts` 只给测试和补录用——线上永远用当前时间。
    """
    uid = canon(uid)
    with connect() as conn:
        conn.execute("INSERT INTO message_log (session, uid, is_group, ts) VALUES (?,?,?,?)",
                     (session, uid, 1 if is_group else 0, ts or now_utc()))


def last_incoming(session: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM message_log WHERE session=? ORDER BY ts DESC LIMIT 1",
            (session,),
        ).fetchone()
    return dict(row) if row else None


def record_proactive(session: str, willingness: float = 0.0, ts: str | None = None,
                     text: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO proactive_log (session, ts, willingness, replied, text) VALUES (?,?,?,0,?)",
            (session, ts or now_utc(), float(willingness), text or ""),
        )
        return int(cur.lastrowid)


def mark_proactive_replied(session: str, uid: str = "") -> None:
    """有人接了她那句主动——标成已回应，并记下是谁、什么时候。

    2026-10-28：原来只有 0/1，页面上只能说"有人接/没人理"，看不出谁接的。
    """
    with connect() as conn:
        conn.execute(
            "UPDATE proactive_log SET replied=1, replied_uid=?, replied_at=? WHERE id=("
            "SELECT id FROM proactive_log WHERE session=? AND replied=0 ORDER BY ts DESC LIMIT 1)",
            (canon(uid), now_utc(), session),
        )


def last_proactive(session: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM proactive_log WHERE session=? ORDER BY ts DESC, id DESC LIMIT 1",
            (session,),
        ).fetchone()
    return dict(row) if row else None


def unanswered_streak(session: str, since_utc: str) -> int:
    """最近这串"发了没人接"的条数。从最新往前数，遇到已回应就停。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT replied FROM proactive_log WHERE session=? AND ts>=? ORDER BY ts DESC, id DESC",
            (session, since_utc),
        ).fetchall()
    n = 0
    for r in rows:
        if r["replied"]:
            break
        n += 1
    return n


def active_events_by_session(session: str, limit: int = 12) -> list[dict]:
    """按会话取未过期事件，时间正序。主动开口时只从这里面找由头。"""
    now = now_utc()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM event WHERE session=? AND (decay_at IS NULL OR decay_at > ?)"
            " ORDER BY ts DESC LIMIT ?",
            (session, now, limit),
        ).fetchall()
    out = []
    for r in reversed(rows):
        d = dict(r)
        d["uid"] = canon(d.get("uid") or "")
        out.append(d)
    return out


# ---------- 好感度（relation） ----------

def bump_relation(uid: str, delta: int = 2, cap: int = 800) -> int:
    """每次互动加一点（默认 +2），封顶 800——档位表按这个量级铺的。"""
    uid = canon(uid)
    if not uid or uid in ("0", "-1"):
        return 0
    ts = now_utc()
    with connect() as conn:
        row = conn.execute("SELECT relation FROM person WHERE uid=?", (uid,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO person (uid, first_seen, last_seen, relation, updated_at)"
                " VALUES (?,?,?,?,?)", (uid, ts, ts, min(cap, delta), ts))
            return min(cap, delta)
        cur = min(cap, int(row["relation"] or 0) + delta)
        conn.execute("UPDATE person SET relation=?, last_seen=?, updated_at=? WHERE uid=?",
                     (cur, ts, ts, uid))
        return cur


def relations(min_relation: int = 1, active_days: int = 14, limit: int = 5) -> list[dict]:
    """聊得来的人：好感度过线，且最近还出现过。她找人说话从这里挑。

    同一个人有多个身份时（QQ / 网页端）在这里合成一条——不然她会以为
    自己认识两个「主人」。先取全量再合并再截断，免得 LIMIT 把正主切掉。
    """
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=active_days)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM person WHERE relation>=? AND last_seen>=?",
            (min_relation, since),
        ).fetchall()]
    merged: dict[str, dict] = {}
    for r in rows:
        u = canon(r.get("uid") or "")
        cur = merged.get(u)
        if cur is None:
            r["uid"] = u
            merged[u] = r
            continue
        cur["relation"] = max(int(cur.get("relation") or 0), int(r.get("relation") or 0))
        cur["last_seen"] = max(cur.get("last_seen") or "", r.get("last_seen") or "")
        if len(r.get("impression") or "") > len(cur.get("impression") or ""):
            cur["impression"] = r.get("impression") or ""
        try:
            names = json.loads(cur.get("names") or "{}")
            names.update(json.loads(r.get("names") or "{}"))
            cur["names"] = json.dumps(names, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
    out = sorted(merged.values(),
                 key=lambda r: (int(r.get("relation") or 0), r.get("last_seen") or ""),
                 reverse=True)
    return out[:limit]


# ---------- 用过的记忆（避免反复提同一件事） ----------

def recent_used_event_ids(uid: str, days: int = 7) -> set[int]:
    """最近 N 天被带进过 prompt 的事件 id。用来给"又说了一遍"降权。"""
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out: set[int] = set()
    uid = canon(uid)
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_ids FROM memory_use WHERE uid=? AND ts>=?", (uid, since)
        ).fetchall()
    for r in rows:
        for part in str(r[0] or "").split(","):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
    return out


# ---------- 按本地日期取当天数据（情绪、日记用） ----------

def _day_bounds_utc(day_local: str) -> tuple[str, str]:
    start = datetime.datetime.strptime(day_local, "%Y-%m-%d").replace(tzinfo=TZ)
    end = start + datetime.timedelta(days=1)
    fmt = "%Y-%m-%d %H:%M:%S"
    return (start.astimezone(datetime.timezone.utc).strftime(fmt),
            end.astimezone(datetime.timezone.utc).strftime(fmt))


def events_on_day(day_local: str) -> list[dict]:
    a, b = _day_bounds_utc(day_local)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM event WHERE ts>=? AND ts<? ORDER BY ts", (a, b)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["uid"] = canon(d.get("uid") or "")
        out.append(d)
    return out


def proactive_on_day(day_local: str) -> list[dict]:
    a, b = _day_bounds_utc(day_local)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM proactive_log WHERE ts>=? AND ts<? ORDER BY ts", (a, b)).fetchall()
    return [dict(r) for r in rows]


def speech_count_on_day(day_local: str) -> int:
    a, b = _day_bounds_utc(day_local)
    with connect() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM speech_log WHERE ts>=? AND ts<?", (a, b)).fetchone()[0])


# ---------- diary ----------

def save_diary(day: str, text: str, mood_label: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO diary (day, text, mood, created_at) VALUES (?,?,?,?)",
            (day, text, mood_label, now_utc()))


def load_diary(day: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM diary WHERE day=?", (day,)).fetchone()
    return dict(row) if row else None


def recent_diaries(limit: int = 5) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM diary ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------- 周记（memory_rollup） ----------

def save_rollup(uid: str, level: str, period: str, summary: str) -> None:
    uid = canon(uid)
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO memory_rollup (uid, level, period, summary, created_at)"
            " VALUES (?,?,?,?,?)",
            (uid, level, period, summary, now_utc()))


def load_rollup(uid: str, level: str, period: str) -> dict | None:
    uid = canon(uid)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM memory_rollup WHERE uid=? AND level=? AND period=?",
            (uid, level, period)).fetchone()
    return dict(row) if row else None


def rollups_for(uid: str, limit: int = 6) -> list[dict]:
    uid = canon(uid)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM memory_rollup WHERE uid=? ORDER BY period DESC LIMIT ?",
            (uid, limit)).fetchall()
    return [dict(r) for r in rows]


def events_between(uid: str, day_from: str, day_to: str) -> list[dict]:
    uid = canon(uid)
    a, _ = _day_bounds_utc(day_from)
    _, b = _day_bounds_utc(day_to)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM event WHERE uid=? AND ts>=? AND ts<? ORDER BY ts",
            (uid, a, b)).fetchall()
    return [dict(r) for r in rows]


def uids_with_events() -> list[str]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT uid FROM event").fetchall()
    out: list[str] = []
    for r in rows:
        u = canon(r[0] or "")
        if u and u not in out:
            out.append(u)
    return out



# ---------- moment_draft（说说稿） ----------

def save_moment(day: str, text: str, mood_label: str = "", image_path: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO moment_draft (day, text, mood, image_path, created_at)"
            " VALUES (?,?,?,?,?)",
            (day, text, mood_label, image_path or "", now_utc()))
        return int(cur.lastrowid)


def recent_moments(limit: int = 10) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM moment_draft ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_moment_status(mid: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE moment_draft SET status=? WHERE id=?", (status, mid))



# ---------- work_log（她动过的活） ----------

def add_work(task: str, trigger: str = "manual", source: str = "self",
             status: str = "ok", note: str = "", actor: str = "clone") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO work_log (task, trigger, actor, source, status, note, ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (task, trigger, actor, source, status, note, now_utc()))
        return int(cur.lastrowid)


def work_since(minutes: int = 10) -> list[dict]:
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM work_log WHERE ts>=? ORDER BY ts, id", (since,)).fetchall()
    return [dict(r) for r in rows]


def recent_work(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM work_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def work_on_day(day_local: str, limit: int = 12) -> list[dict]:
    """当天她/分身做过的事，按时间正序。回答"谁弄的"要靠这个。"""
    a, b = _day_bounds_utc(day_local)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM work_log WHERE ts>=? AND ts<? ORDER BY ts, id LIMIT ?",
            (a, b, limit)).fetchall()
    return [dict(r) for r in rows]


def work_recent_days(days: int = 7, limit: int = 40) -> list[dict]:
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM work_log WHERE ts>=? ORDER BY ts DESC, id DESC LIMIT ?",
            (since, limit)).fetchall()
    return [dict(r) for r in rows]


def get_moment(mid: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM moment_draft WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


# ---------- push_log（定时推送去重） ----------

def mark_push(day: str, kind: str) -> bool:
    """占位。第一次返回 True（该发了），重复返回 False。"""
    try:
        with connect() as conn:
            conn.execute("INSERT INTO push_log (day, kind, ts) VALUES (?,?,?)",
                         (day, kind, now_utc()))
        return True
    except Exception:  # noqa: BLE001
        return False


def pushed(day: str, kind: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM push_log WHERE day=? AND kind=?",
                           (day, kind)).fetchone()
    return bool(row)


def session_is_group(session: str) -> int | None:
    """这条会话是群吗？1=群 0=私聊 None=没见过（没法判断，别硬猜）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT is_group FROM message_log WHERE session=? ORDER BY id DESC LIMIT 1",
            (session,)).fetchone()
    return int(row[0]) if row else None


def delete_moment(mid: int) -> bool:
    """真删一条说说稿。返回是否删到了东西。"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM moment_draft WHERE id=?", (mid,))
        return cur.rowcount > 0


# ---------- 记忆检索（记忆变多之后靠这个看） ----------

def search_events(q: str = "", sentiment: str = "", min_importance: int = 0,
                  offset: int = 0, limit: int = 40,
                  uid: str = "") -> tuple[list[dict], int]:
    """按关键词/情绪/重要度筛选，返回 (本页, 总数)。

    q 同时匹配 summary 和 keywords——她记得的事，人名和关键词常在两边各半。
    """
    where, params = [], []
    if uid:
        where.append("uid = ?")
        params.append(uid)
    if q:
        where.append("(summary LIKE ? OR keywords LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if sentiment in ("正", "中", "负"):
        where.append("sentiment = ?")
        params.append(sentiment)
    if min_importance > 0:
        where.append("importance >= ?")
        params.append(int(min_importance))

    cond = (" WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM event{cond}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM event{cond} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)]).fetchall()
    return [dict(r) for r in rows], total


def update_event(eid: int, fields: dict) -> bool:
    """改一条记忆。只允许改动白名单字段，别的不认。

    2026-10-28：页面要能改摘要和重要度。原先只有删除，抽错了只能删了重写。
    """
    allowed_keys = ('summary', 'importance', 'kind', 'sentiment', 'keywords')
    data = {k: v for k, v in (fields or {}).items() if k in allowed_keys and v is not None}
    if not data:
        return False
    # 重要度必须落在 1-5：页面有下拉框限制，直接调接口却能塞 99，
    # 界面的五颗点会画错（2026-10-28 顺手补的校验）
    if "importance" in data:
        try:
            data["importance"] = max(1, min(5, int(data["importance"])))
        except (TypeError, ValueError):
            data.pop("importance")
        if not data:
            return False
    if "sentiment" in data and str(data["sentiment"]) not in ("正", "中", "负"):
        data.pop("sentiment")
        if not data:
            return False
    sets = ', '.join(f'{k}=?' for k in data)
    with connect() as conn:
        cur = conn.execute(f'UPDATE event SET {sets} WHERE id=?', (*data.values(), int(eid)))
        return cur.rowcount > 0


def delete_event(eid: int) -> bool:
    """删一条记忆。她忘掉这件事——不是"不再提"，是真的从库里拿走。"""
    with connect() as conn:
        cur = conn.execute("DELETE FROM event WHERE id=?", (int(eid),))
        return cur.rowcount > 0
