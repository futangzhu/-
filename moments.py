"""说说：日记就是稿子。

2026-10-26 03:14 主人定的分工：

    写日记、审日记     在我们自己的插件里完成
    配图              交给生图插件（插件市场的通用接口）
    发布              交给发布插件（space_kit），再由主人确认

所以这一层只做三件事：把日记变成一条待审的稿、调生图拿一张配图、需要时把稿子
送进发布箱。文本一律来自 `diary`（她本体写的），不另写一份。
"""

from __future__ import annotations

import os
import sqlite3
import time

from . import diary, paths, scenes, store

SPACE_DB = ""   # 运行时取 paths.space_db()


async def ensure_from_diary(provider, context, day: str | None = None,
                            with_image: bool = True, reuse: bool = True) -> dict:
    """把当天的日记变成一条候选稿（没有日记就先写日记）。

    `reuse`：当天已经有待审的稿就直接复用，别每次点都生成一条新的。
    之前连点两次会得到两条一样的稿，是没人会想要的行为。
    """
    day = day or store.today_local()
    if reuse:
        for r in store.recent_moments(10):
            if r.get("day") == day and r.get("status") == "draft":
                return {"id": r["id"], "day": day, "text": r["text"],
                        "image_path": r.get("image_path") or "", "scene": "已有稿件"}
    di = store.load_diary(day)
    if not di:
        di = await diary.generate(provider, day)

    image, scene = ("", "")
    if with_image and context is not None:
        image, scene = await scenes.make(context)

    mid = store.save_moment(day, di["text"], di.get("mood") or "", image)
    return {
        "id": mid, "day": day, "text": di["text"],
        "image_path": image, "scene": scene,
    }


async def remake_image(context, mid: int) -> tuple[str, str]:
    """给某条稿重挑一张配图（不满意就重画一张）。"""
    row = store.get_moment(mid)
    if not row:
        return "", ""
    image, scene = await scenes.make(context)
    if not image:
        return "", ""
    with store.connect() as conn:
        conn.execute("UPDATE moment_draft SET image_path=? WHERE id=?", (image, mid))
    return image, scene


def approve(mid: int) -> bool:
    """审过了。审之前谁也别发。"""
    row = store.get_moment(mid)
    if not row:
        return False
    store.set_moment_status(mid, "approved")
    return True


def to_space_draft(text: str, image_path: str = "", note: str = "",
                   forward_target: str = "") -> int | None:
    """送进 space_kit 的草稿箱，等主人 `/动态发 <id>` 确认发布。

    只写草稿、不发布。库不在就安静返回 None。
    """
    SPACE_DB = paths.space_db()
    if not os.path.exists(SPACE_DB):
        return None
    try:
        con = sqlite3.connect(SPACE_DB, timeout=10)
        cur = con.execute(
            "INSERT INTO drafts(text, source, subject_id, subject_name, forward_target,"
            " created_at, forward_note, image_path) VALUES (?,?,?,?,?,?,?,?)",
            (text, "life_memory", "", "她的一天", forward_target or "",
             int(time.time()), (note or "她自己的日记"), image_path or ""),
        )
        did = int(cur.lastrowid)
        con.execute(
            "INSERT INTO events(kind, draft_id, detail, created_at) VALUES (?,?,?,?)",
            ("draft_created", did, f"日记当稿：{text[:60]}", int(time.time())),
        )
        con.commit()
        con.close()
        return did
    except Exception:  # noqa: BLE001
        return None
