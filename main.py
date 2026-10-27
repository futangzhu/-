"""life_memory —— 她的一天，和她记得的人。

M1 阶段：只做库与检索，不接线上对话。
本插件是 memory.db 的唯一写入方（数据模型.md §7.4）。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import sqlite3
import time

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.star import Context, Star
from quart import jsonify, request

from . import (dayplan, diary, extract, gate, mood, moments, persona, proactivity,
               recall, paths, relation, rollup, scenes, speak, state, store, voice, work)

LOG = logging.getLogger("astrbot")
PLUGIN_NAME = "astrbot_plugin_life_memory"
CONFIG_PATH = ""   # 运行时取 paths.config_path()


class LifeMemory(Star):
    # 2026-10-28：AstrBot 重载插件时**不会**调 terminate（实测：日志里没有那句
    # 「后台循环已停止」），旧实例的后台任务就这么一直活着。重载几次就有几个
    # 循环在跑，主动开口/晚报/日记都会重复。用一个全局序号让旧循环自己退出。
    _epoch = 0

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        LifeMemory._epoch += 1
        self._epoch = LifeMemory._epoch
        self.config = config or {}
        self._day_task: asyncio.Task | None = None
        self._last_extract: dict[str, float] = {}   # session → 上次抽取的时刻（节流用）
        self._speak_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None   # 掉线后补发私聊消息
        # 给别的插件用的口吻接口：拿不到就返回空，由调用方兜底
        from types import SimpleNamespace
        self.voice_api = SimpleNamespace(say=self._voice_say)
        self._extract_budget: dict[str, tuple[str, int]] = {}   # session → (小时, 已用次数)
        self._name_cache: tuple[float, dict[str, str]] = (0.0, {})
        self._umo_cache: dict[str, str] = {}   # 会话 id → unified_msg_origin（发消息要三段式）

    async def initialize(self) -> None:
        try:
            store.set_timezone(self.config.get("timezone") or "Asia/Shanghai")
            persona.set_from_config(self.config)
            store.init_db()
            dayplan.ensure_prompt_file()
            self._register_web_apis()
            if self._speak_task is None or self._speak_task.done():
                self._speak_task = asyncio.create_task(self._proactive_loop())
            if self._retry_task is None or self._retry_task.done():
                self._retry_task = asyncio.create_task(self._retry_loop())
            LOG.info(f"[life_memory] 库就绪：{store.db_path()}")
        except Exception as e:  # noqa: BLE001
            LOG.error(f"[life_memory] 建库失败：{e}")

    async def terminate(self) -> None:
        """重载/卸载时把后台循环收掉。

        2026-10-28 补：以前没有这个方法，每次 reload 都会再起一个 _proactive_loop，
        旧的那个仍然活着（它拿着旧实例的引用继续跑）。重载四次就是四个循环同时跑，
        主动开口、晚报、日记都会重复。分享出去以后别人也会遇到。
        """
        try:
            await super().terminate()
        except (AttributeError, TypeError):
            pass
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"[life_memory] 父类 terminate 报错：{e}")
        for name in ("_speak_task", "_retry_task", "_day_task"):
            task = getattr(self, name, None)
            setattr(self, name, None)
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                LOG.warning(f"[life_memory] 停 {name} 时出错：{e}")
        LOG.info("[life_memory] 后台循环已停止（重载/卸载）")

    # ---------- 会话字符串 ----------

    @staticmethod
    def _sender_name(event) -> str:
        """平台给的发送者昵称，取不到就空。昵称只认这个，不认模型推断。"""
        for getter in ("get_sender_name", "get_sender_nickname"):
            try:
                value = getattr(event, getter, None)
                if callable(value):
                    name = str(value() or "").strip()
                    if name:
                        return name[:20]
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _learn_umo(self, event) -> None:
        """顺手记住 sid 与 umo 的对应。事件里两样都有，比猜可靠。"""
        try:
            sid = str(event.get_session_id() or "")
            umo = getattr(event, "unified_msg_origin", "") or ""
            if sid and umo.count(":") >= 2:
                self._umo_cache[sid] = umo
        except Exception:  # noqa: BLE001
            pass

    def _platform_ids(self) -> list[str]:
        """平台实例 id，QQ 那一类排前面。

        注意：实例 id 取的是配置里那个名字（本机是"账号1"），不是适配器类型名
        "aiocqhttp"。所以不能硬写 aiocqhttp，那会拼出一个平台找不到的 umo。
        """
        ids: list[str] = []
        try:
            insts = list(self.context.platform_manager.platform_insts)

            def _rank(inst) -> int:
                name = str(getattr(inst.meta(), "name", "")).lower()
                return 0 if ("aiocqhttp" in name or "qq" in name) else 1

            for inst in sorted(insts, key=_rank):
                mid = str(getattr(inst.meta(), "id", "") or "")
                if mid and mid not in ids:
                    ids.append(mid)
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"[life_memory] 取平台实例失败：{e}")
        return ids or ["aiocqhttp"]

    def umo(self, sid: str, default_group: bool = False) -> str:
        """把半截会话 id 补成 platform:类型:id（只用来展示/兜底，发消息请用 _send）。"""
        sid = str(sid or "").strip()
        if sid.count(":") >= 2:
            return sid
        if sid in self._umo_cache:
            return self._umo_cache[sid]
        known = store.session_is_group(sid)
        is_group = default_group if known is None else bool(known)
        kind = "GroupMessage" if is_group else "FriendMessage"
        return f"{self._platform_ids()[0]}:{kind}:{sid}"

    async def _send(self, sid: str, chain, default_group: bool = False) -> bool:
        """把消息发到某个会话，半截 id 也认。

        2026-10-26 踩过的坑：配置里 `proactive_sessions` 写的是群号本身
        （群号），而 `context.send_message` 要的是三段式 umo；
        `MessageSession.from_str` 按 ":" 切三段，切不开就抛"不合法的 session 字符串"。
        主动循环每 5 分钟炸一次，话已经写出来了，卡在最后一步丢掉。

        现在：先试学到过的 umo，再按平台实例逐个拼着试，成功就记下来，
        下次直接用。宁可多试一次，也不要再让它写完了发不出去。
        """
        sid = str(sid or "").strip()
        if not sid:
            return False

        is_group = self._is_group_target(sid, default_group)

        # 2026-10-28：QQ 掉线时 context.send_message 可能「成功返回但消息没出去」，
        # 晚报就是这么丢的（日志写着已发，人没收到）。发之前先探活。
        if self.config.get("online_check", True) and not await self._platform_online():
            return await self._handle_offline(sid, chain, is_group)

        try:
            ok = await self._raw_send(sid, chain, default_group)
            if ok:
                return True
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"[life_memory] 发不出去（{sid}）：{exc}")
            return await self._handle_offline(sid, chain, is_group)

        # 所有候选 umo 都失败：按离线处理（私聊入队，群聊丢弃）
        return await self._handle_offline(sid, chain, is_group)

    def _is_group_target(self, sid: str, default_group: bool) -> bool:
        if sid.count(":") >= 2:
            return "GroupMessage" in sid
        known = store.session_is_group(sid)
        return default_group if known is None else bool(known)

    async def _raw_send(self, sid: str, chain, default_group: bool = False) -> bool:
        """真正发出去，不做探活。返回是否成功。"""
        if sid.count(":") >= 2:
            await self.context.send_message(sid, chain)
            return True

        known = store.session_is_group(sid)
        is_group = default_group if known is None else bool(known)
        kind = "GroupMessage" if is_group else "FriendMessage"
        cands: list[str] = []
        if sid in self._umo_cache:
            cands.append(self._umo_cache[sid])
        cands += [f"{pid}:{kind}:{sid}" for pid in self._platform_ids()]

        last: Exception | None = None
        for u in dict.fromkeys(cands):
            try:
                await self.context.send_message(u, chain)
                self._umo_cache[sid] = u
                return True
            except Exception as e:  # noqa: BLE001
                last = e
        LOG.error(f"[life_memory] 发不出去（{sid}）：{last}")
        return False

    # ---------- 掉线处理：私聊入队重发，群聊直接丢 ----------

    def _chain_text(self, chain) -> str:
        """只接受纯文本链；带图片等组件的消息不入队（存不了）。"""
        parts = []
        for comp in getattr(chain, "chain", []) or []:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            else:
                return ""
        return "".join(parts).strip()

    async def _handle_offline(self, sid: str, chain, is_group: bool) -> bool:
        text = self._chain_text(chain)
        if not text:
            LOG.warning(f"[life_memory] 平台离线，非纯文本消息丢弃（{sid}）")
            return False
        if is_group:
            # 群里的话是「当下」的：半夜的「该睡觉了」第二天早上送到只会更蠢
            LOG.warning(f"[life_memory] 平台离线，群消息丢弃（不补发）{sid}：{text[:24]}")
            return False
        self._enqueue(sid, text)
        LOG.warning(f"[life_memory] 平台离线，私聊消息已入队 {sid}：{text[:24]}")
        return False

    def _pending_path(self) -> str:
        return os.path.join(paths.plugin_dir(), "pending_messages.json")

    def _load_pending(self) -> list[dict]:
        try:
            with open(self._pending_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001
            return []

    def _save_pending(self, items: list[dict]) -> None:
        try:
            os.makedirs(os.path.dirname(self._pending_path()), exist_ok=True)
            with open(self._pending_path(), "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"[life_memory] 待发队列写盘失败：{exc}")

    def _enqueue(self, sid: str, text: str) -> None:
        items = self._load_pending()
        items.append({"sid": sid, "text": text, "ts": time.time()})
        self._save_pending(items)

    async def _platform_online(self) -> bool:
        """探活：调一次 get_login_info。没有 aiocqhttp 实例就当作在线，别误伤。"""
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
                AiocqhttpAdapter,
            )
        except Exception:  # noqa: BLE001
            return True
        try:
            insts = list(self.context.platform_manager.platform_insts)
        except Exception:  # noqa: BLE001
            return True
        adapters = [i for i in insts if isinstance(i, AiocqhttpAdapter)]
        if not adapters:
            return True
        for inst in adapters:
            try:
                client = inst.get_client()
                if client is None:
                    continue
                info = await asyncio.wait_for(client.call_action("get_login_info"), timeout=5)
                if info and (info.get("user_id") or info.get("data")):
                    return True
            except Exception as exc:  # noqa: BLE001
                LOG.debug(f"[life_memory] 探活失败：{exc}")
        return False

    async def _flush_pending(self) -> None:
        items = self._load_pending()
        if not items:
            return
        if not await self._platform_online():
            return
        expire = int(self.config.get("pending_expire_minutes", 180) or 0)
        now = time.time()
        keep: list[dict] = []
        for item in items:
            if expire > 0 and now - float(item.get("ts", 0)) > expire * 60:
                LOG.warning(f"[life_memory] 待发消息过期丢弃：{str(item.get('text'))[:24]}")
                continue
            try:
                ok = await self._raw_send(str(item.get("sid")), MessageChain([Plain(str(item.get("text")))]))
            except Exception as exc:  # noqa: BLE001
                ok = False
                LOG.warning(f"[life_memory] 补发失败：{exc}")
            if ok:
                LOG.info(f"[life_memory] 补发成功 -> {item.get('sid')}：{str(item.get('text'))[:24]}")
            else:
                keep.append(item)
        self._save_pending(keep)

    async def _retry_loop(self) -> None:
        # 启动后先等一会儿，别和插件初始化的其它事抢；之后按配置间隔扫
        await asyncio.sleep(15)
        while True:
            if self._epoch != LifeMemory._epoch:
                LOG.info("[life_memory] 补发循环退场（插件已重载）")
                return
            try:
                await self._flush_pending()
            except Exception as exc:  # noqa: BLE001
                LOG.debug(f"[life_memory] 补发循环异常：{exc}")
            interval = int(self.config.get("retry_interval_seconds", 300) or 300)
            await asyncio.sleep(max(30, interval))


    def _maybe_generate_day(self) -> None:
        """当天没有计划就在后台补一份，不阻塞当前这条消息。

        生成要调模型，十几秒，放在请求路径里那条消息会明显卡。
        代价是：今天最早那几条消息可能还没有活动块，从第二条起才有。
        """
        try:
            if state.is_asleep():
                return
            if dayplan.load_day(store.today_local()):
                return
            if self._day_task and not self._day_task.done():
                return
            provider = self.context.get_using_provider()
            if provider is None:
                LOG.warning("[life_memory] 没有可用 provider，今日计划无法生成")
                return
            self._day_task = asyncio.create_task(self._generate_day(provider))
        except Exception as e:  # noqa: BLE001
            LOG.error(f"[life_memory] 触发日计划失败：{e}")

    async def _generate_day(self, provider) -> None:
        try:
            got = await dayplan.ensure_day(provider)
        except Exception as e:  # noqa: BLE001
            LOG.error(f"[life_memory] 生成日计划异常：{e}")
            return
        if got:
            LOG.info(f"[life_memory] 今日计划已生成：{len(got['blocks'])} 块")
        else:
            LOG.warning(f"[life_memory] 今日计划生成失败：{dayplan.LAST_ERROR}")

    # ---------- M2：状态块注入 ----------

    @filter.on_llm_request(priority=1500)
    async def inject_state(self, event: AstrMessageEvent, req) -> None:
        """在她开口之前补一句"此刻"。

        排在 AstrNa(1000) 之后，免得这段被它的上下文裁剪顺手带走。
        正文一个字不改，只往 system_prompt 末尾追加。
        """
        try:
            self._learn_umo(event)
            self._maybe_generate_day()
            disturbed = bool(getattr(event, "is_at_or_wake_command", False))
            if disturbed and state.is_asleep():
                store.record_wake(event.get_session_id())
            uid_now = str(event.get_sender_id())
            masters = {str(x) for x in (self.config.get("master_ids") or [])}
            block = state.build(event.get_session_id(), disturbed=disturbed,
                                user_text=getattr(event, "message_str", "") or "",
                                is_master=uid_now in masters)
        except Exception as e:  # noqa: BLE001
            LOG.error(f"[life_memory] 状态块生成失败：{e}")
            return
        if not block:
            return
        base = getattr(req, "system_prompt", "") or ""
        req.system_prompt = f"{base}\n\n{block}" if base else block

        # 记忆注入：这才是"她记得住"真正生效的地方。
        # 预算单独限死 80 字——状态块管"此刻"，记忆管"从前"，两样加起来不能压过人格本身。
        try:
            if state.is_asleep():
                return
            user_text = getattr(event, "message_str", "") or ""
            uid = event.get_sender_id()
            mem = recall.retrieve(uid, user_text, k=3, budget=80)
            if mem["text"]:
                req.system_prompt += f"\n\n【她记得的】{mem['text']}"
                store.log_use(uid, event.get_session_id(), mem["ids"])
        except Exception as e:  # noqa: BLE001
            LOG.debug(f"[life_memory] 记忆注入失败：{e}")

    @filter.on_llm_response(priority=1500)
    async def record_speech(self, event: AstrMessageEvent, response) -> None:
        """记下"她说过话"，顺手把这一轮里值得记的事抽出来（M4）。"""
        try:
            text = getattr(response, "completion_text", "") or ""
            sid = event.get_session_id()
            # 2026-10-28：网页端（chatui）也是真的在跟她说话，不再当"开发说明"跳过。
            # 顺手记下 uid，统计和检索才能分清是跟谁说的。
            try:
                uid = str(event.get_sender_id())
            except Exception:  # noqa: BLE001
                uid = ""
            store.record_speech(sid, len(text), text=text, uid=uid)
            self._maybe_extract(event, sid, text)
        except Exception as e:  # noqa: BLE001
            LOG.debug(f"[life_memory] 记录发言失败：{e}")

    # ---------- 网页端历史回填 ----------

    def _backfill_state_path(self) -> str:
        return os.path.join(paths.plugin_dir(), "backfill_state.json")

    def _read_backfill_state(self) -> dict:
        try:
            with open(self._backfill_state_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_backfill_state(self, data: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self._backfill_state_path()), exist_ok=True)
            with open(self._backfill_state_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(f"[life_memory] 回填进度写盘失败：{exc}")

    @staticmethod
    def _plain_of(content) -> str:
        """从 platform_message_history.content 里抠出纯文本。

        结构是 {"type": "user"|"bot", "message": [{"type": "plain", "text": ...}, ...]}，
        图片、语音、工具调用这些一律跳过——她记的是话，不是附件。
        """
        try:
            data = json.loads(content) if isinstance(content, str) else (content or {})
        except Exception:  # noqa: BLE001
            return ""
        parts = []
        for comp in (data.get("message") or []):
            if str(comp.get("type")) == "plain" and comp.get("text"):
                parts.append(str(comp["text"]))
        return "".join(parts).strip()

    @filter.command("回填网页历史")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def backfill_web_history(self, event: AstrMessageEvent):
        """把网页端聊过的内容补进她的记忆。

        为什么需要：网页端以前当运维面板，`record_speech` 直接跳过，
        于是你在那边说的话她一句都记不住。现在改成真聊天了，历史要补上。

        为什么要限量：每对「你说 + 她答」都是一次模型调用，4000 多条全跑既贵又慢。
        默认 100 对，最多 500；进度落盘，重复执行接着上次跑。
        """
        parts = event.message_str.strip().split(maxsplit=1)
        try:
            limit = int(parts[1]) if len(parts) > 1 else 100
        except (ValueError, IndexError):
            limit = 100
        limit = max(1, min(limit, 500))

        db_path = os.path.join(paths.data_root(), "data_v4.db")
        if not os.path.exists(db_path):
            yield event.plain_result(f"找不到主库：{db_path}")
            return

        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用的模型，抽不了记忆。")
            return

        state = self._read_backfill_state()
        last_id = int(state.get("last_id", 0) or 0)
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            rows = conn.execute(
                "SELECT id, sender_id, sender_name, content FROM platform_message_history "
                "WHERE platform_id LIKE 'webchat%' AND id > ? ORDER BY id LIMIT ?",
                (last_id, max(limit * 4, 40)),
            ).fetchall()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            LOG.error(f"[life_memory] 回填读库失败：{exc}")
            yield event.plain_result(f"读主库失败：{exc}")
            return

        if not rows:
            yield event.plain_result("没有新的网页端记录了（已经补到底）。")
            return

        yield event.plain_result(f"开始回填：从 id={last_id} 起，最多 {limit} 对，可能要几分钟…")

        done = 0
        failed = 0
        events_total = 0
        pending_user = ""
        pending_uid = ""

        for row in rows:
            if done >= limit:
                break
            rid, sender_id, sender_name, content = row[0], row[1], row[2], row[3]
            text = self._plain_of(content)
            if not text:
                last_id = rid
                continue

            is_bot = str(sender_id) == "bot" or str(sender_name) == "bot"
            if not is_bot:
                pending_user = text
                pending_uid = store.canon(str(sender_id or sender_name or ""))
                last_id = rid
                continue

            if not pending_user:
                last_id = rid
                continue

            try:
                person, events = await extract.extract(provider, pending_user, text)
                events_total += extract.save(pending_uid, "webchat", person, events,
                                             sender_name=str(row[2] or ""))
                done += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                LOG.warning(f"[life_memory] 回填第 {done + failed} 对失败：{exc}")
            pending_user = ""
            last_id = rid
            self._write_backfill_state({"last_id": last_id, "done": state.get("done", 0) + done})
            if done and done % 10 == 0:
                LOG.info(f"[life_memory] 回填进度 {done} 对，事件 {events_total} 条")

        self._write_backfill_state(
            {"last_id": last_id, "done": int(state.get("done", 0) or 0) + done}
        )
        LOG.info(f"[life_memory] 回填结束：{done} 对成功，{failed} 对失败，新增事件 {events_total}")
        yield event.plain_result(
            f"回填完成：{done} 对成功，{failed} 对失败，新增记忆事件 {events_total} 条。\n"
            f"进度已存到 id={last_id}，再发一次会接着往后跑。"
        )

    def _maybe_extract(self, event: AstrMessageEvent, session: str, bot_text: str) -> None:
        """抽记忆：后台跑，不拖慢回复；同会话一分钟最多一次。

        为什么节流：抽取是一次完整的模型调用，连着聊十句就抽十次，既贵又没必要——
        同一段对话里的信息，抽一次基本就够。
        """
        if not self.config.get("extract_enabled", False):
            return                      # 默认关闭：先手动 /抽一次 验证，再开自动
        # 白名单：显式列出的会话一定抽；私聊可以整体放开（extract_private_all）。
        # 面板会话（webchat）永远不抽——那是我跟主人的开发对话，进了库就全是项目术语。
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo.startswith("webchat"):
            return
        allowed = {str(x) for x in (self.config.get("extract_sessions") or [])}
        if session not in allowed:
            is_private = ":FriendMessage:" in umo
            if not (self.config.get("extract_private_all", False) and is_private):
                return
        user_text = getattr(event, "message_str", "") or ""
        if extract.should_skip(user_text):
            return
        now = asyncio.get_event_loop().time()
        if now - self._last_extract.get(session, 0) < 60:
            return
        # 每小时上限：一分钟一次的节流理论上一天能抽上千次，实际群里没那么密，
        # 但热群一晚上聊几百条就会顶到这个数。10 次/小时足够覆盖，也压得住账单。
        hour = datetime.datetime.now(store.TZ).strftime("%Y-%m-%d %H")
        cur_hour, used = self._extract_budget.get(session, (hour, 0))
        if cur_hour != hour:
            cur_hour, used = hour, 0
        if used >= int(self.config.get("extract_hourly_limit", 10)):
            self._extract_budget[session] = (cur_hour, used)
            return
        self._extract_budget[session] = (cur_hour, used + 1)
        provider = self.context.get_using_provider()
        if provider is None:
            return
        self._last_extract[session] = now
        asyncio.create_task(
            self._extract_async(provider, session, event.get_sender_id(), user_text, bot_text,
                                self._sender_name(event))
        )

    async def _extract_async(self, provider, session: str, uid: str,
                             user_text: str, bot_text: str,
                             sender_name: str = "") -> None:
        try:
            person, events = await extract.extract(provider, user_text, bot_text)
            n = extract.save(uid, session, person, events, sender_name=sender_name)
            if n:
                work.log("顺手记了他刚说的事", "passive")
                LOG.info(f"[life_memory] 抽到 {n} 条记忆（{session}）")
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"[life_memory] 抽取失败：{e}")


    async def _voice_say(self, kind: str, info: dict | None = None, session: str = "") -> str:
        """别的插件来要"她的一句话"。失败返回空字符串。"""
        return await voice.say(self.context.get_using_provider(), kind, info or {}, session)


    # ---------- 晚报（睡前找主人说一句） ----------

    def _evening_info(self) -> dict:
        today = store.today_local()
        blocks = store.blocks_for(today)
        picked = [b for b in blocks if b.get("origin") == "random"][:1] + \
                 [b for b in blocks if b.get("origin") != "random"][:2]
        acts = "；".join(b["activity"] for b in picked) if picked else ""
        events = store.events_on_day(today)
        info = {}
        if acts:
            info["今天做过"] = acts
        try:
            info["现在的心情"] = mood.score()["label"]
        except Exception:  # noqa: BLE001
            pass
        if events:
            info["今天记下的"] = "；".join(e["summary"] for e in events[:2])
        di = store.load_diary(today)
        if di and di.get("text"):
            info["你自己晚上写的"] = di["text"][:60]
        return info

    async def _maybe_evening(self) -> None:
        """到点给她自己发一条晚报。一天一条，重启也不重复。"""
        if not self.config.get("evening_report_enabled", False):
            return
        target = str(self.config.get("evening_report_target") or "")
        if not target:
            return
        now = datetime.datetime.now(store.TZ)
        at = str(self.config.get("evening_report_time") or "23:00")
        if now.strftime("%H:%M") < at or state.is_asleep(now):
            return
        day = store.today_local()
        if store.pushed(day, "evening"):
            return
        provider = self.context.get_using_provider()
        if provider is None:
            return
        text = await voice.say(provider, "evening", self._evening_info(), session=target)
        if not text:
            LOG.info("[life_memory] 晚报没写出来，今天先不发")
            return
        if not store.mark_push(day, "evening"):
            return
        # 2026-10-28：以前不看返回值，掉线时照样打「已发」，把没发出去说成发了
        ok = await self._send(target, MessageChain([Plain(text)]))
        if ok:
            LOG.info(f"[life_memory] 晚报已发：{text[:30]}")
        else:
            LOG.warning(f"[life_memory] 晚报没发出去（已按离线规则处理）：{text[:30]}")

    # ---------- 主动开口（M6） ----------

    async def _proactive_loop(self) -> None:
        """每 5 分钟看一次"现在能不能自己说句话"。默认关闭，配置打开才会真的发。"""
        await asyncio.sleep(90)
        while True:
            if self._epoch != LifeMemory._epoch:
                LOG.info("[life_memory] 主动循环退场（插件已重载）")
                return
            try:
                await self._maybe_diary()
                await self._maybe_evening()
                await self._proactive_tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                LOG.error(f"[life_memory] 主动循环异常：{e}")
            await asyncio.sleep(300)

    @staticmethod
    def _minus_hour(hhmm: str) -> str:
        """"23:00" → "22:00"。跨零点也对："00:30" → "23:30"。"""
        try:
            h, m = (int(x) for x in str(hhmm).strip().split(":")[:2])
        except Exception:  # noqa: BLE001
            return str(hhmm)
        t = (h * 60 + m - 60) % (24 * 60)
        return f"{t // 60:02d}:{t % 60:02d}"

    async def _maybe_diary(self) -> None:
        """晚上写当天，早上补漏。

        2026-10-26 04:20 主人的一句话让我改了顺序：日记本来就该晚上写——
        材料齐全，写完顺手备成说说稿，23:00 的晚报再接着说一句，是一条线。
        早上那次只做兜底：昨晚没写成（断电、重载、模型抽风）就补昨天那一篇。
        """
        now = datetime.datetime.now(store.TZ)
        if state.is_asleep(now):
            return
        provider = self.context.get_using_provider()
        if provider is None:
            return

        today = store.today_local()
        at = str(self.config.get("diary_hour") or "22:30").strip()
        # 半成品重写的检查点，独立于日记时间：默认挂在"发布前一小时"。
        # 2026-10-27 主人要的：万一要测要改，得在发出去之前留出这一小时。
        publish_at = str(self.config.get("diary_publish_hour") or "23:00").strip()
        rewrite_at = (str(self.config.get("diary_rewrite_hour") or "").strip()
                      or self._minus_hour(publish_at))

        # ① 晚上到点：写今天的
        existing = store.load_diary(today)
        # 口径：早于「发布前一小时」写的那篇不算数。cutoff 就是那一个小时点。
        try:
            _h, _mi = (int(x) for x in rewrite_at.split(":")[:2])
            cutoff = now.replace(hour=_h, minute=_mi, second=0, microsecond=0)
        except Exception:  # noqa: BLE001
            cutoff = None
        premature = diary.too_early(existing, cutoff)
        hhmm = now.strftime("%H:%M")
        need = (hhmm >= at and existing is None) or (premature and hhmm >= rewrite_at)
        if need:
            if getattr(self, "_diary_day", "") == today and not premature:
                return
            # 同一天只重写一次。发布点要是挪到凌晨，created_at 的"小时<12"会一直成立，
            # 不设这道闸它每 5 分钟重写一遍。
            if premature and getattr(self, "_diary_rewrite_day", "") == today:
                return
            self._diary_day = today
            if premature:
                self._diary_rewrite_day = today
            try:
                got = await diary.generate(provider, today)
            except Exception as e:  # noqa: BLE001
                LOG.warning(f"[life_memory] 今晚日记没写成：{e}")
                return
            work.log("写了今天的日记", "scheduled", actor="self")
            LOG.info(f"[life_memory] 今天的日记{'重写' if premature else '已写'}"
                     f"（{len(got['text'])} 字｜检查点 {rewrite_at}，发布 {publish_at}）")
            if self.config.get("moment_auto", False):
                try:
                    m = await moments.ensure_from_diary(provider, self.context, today)
                    work.log("把日记配了张图，备成待审的稿", "scheduled")
                    LOG.info(f"[life_memory] 说说稿 #{m['id']} 已备好")
                except Exception as e:  # noqa: BLE001
                    LOG.warning(f"[life_memory] 说说稿没备成：{e}")
            return

        # ② 早上兜底：昨晚没写成的补一篇
        if getattr(self, "_diary_day", "") == today:
            return
        self._diary_day = today
        try:
            made = rollup.ensure_last_week()
            if made:
                LOG.info(f"[life_memory] 补了 {len(made)} 条周记")
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"[life_memory] 周记失败：{e}")

        got = await diary.ensure_yesterday(provider)
        if got:
            work.log("补了昨天没写的日记", "scheduled", actor="self")
            LOG.info(f"[life_memory] 补了 {got['day']} 的日记（{len(got['text'])} 字）")
            if self.config.get("moment_auto", False):
                try:
                    await moments.ensure_from_diary(provider, self.context, got["day"])
                    work.log("把日记备成了待审的稿", "scheduled")
                except Exception as e:  # noqa: BLE001
                    LOG.warning(f"[life_memory] 说说稿没备成：{e}")


    async def _proactive_tick(self) -> None:
        if not self.config.get("proactive_enabled", False):
            return
        sessions = self.config.get("proactive_sessions") or []
        if not sessions:
            return
        provider = self.context.get_using_provider()
        if provider is None:
            return

        for session in sessions:
            try:
                r = gate.can_speak(session, enabled=True)
                if not r["ok"]:
                    continue
                text = await speak.compose(provider, session)
                if not text:
                    LOG.info(f"[life_memory] 闸门放行但没写出合适的话（{session}）")
                    continue
                await self._send(session, MessageChain([Plain(text)]), default_group=True)
                speak.remember_spoken(session, text, r["score"])
                LOG.info(f"[life_memory] 主动发言（{session}｜意愿 {r['score']}）：{text}")
            except Exception as e:  # noqa: BLE001
                # 一个会话发不出去，不该拖死后面那些
                LOG.error(f"[life_memory] 主动发言失败（{session}）：{e}")

        # 私聊：她主动去找聊得来的人，不是只找主人。
        # 一轮最多找一个人，且同一人一天最多一次——她不是发通知的。
        if not self.config.get("proactive_private_enabled", False):
            return
        for person in store.relations(limit=3):
            uid = person["uid"]
            r = gate.can_speak_private(uid, enabled=True, relation=int(person.get("relation") or 0))
            if not r["ok"]:
                continue
            text = await speak.compose(provider, uid)
            if not text:
                continue
            await self._send(uid, MessageChain([Plain(text)]))
            speak.remember_spoken(uid, text, r["score"])
            LOG.info(f"[life_memory] 私聊主动（{uid}｜好感 {person.get('relation')}）：{text}")
            break

    # ---------- 查看与补生成 ----------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("周记", alias={"看看周记"})
    async def show_rollup(self, event: AstrMessageEvent):
        rows = store.rollups_for(event.get_sender_id(), limit=6) or []
        if not rows:
            yield event.plain_result("还没有周记（每周一醒来后会自动补上周的）")
            return
        out = [f"{r['period']}\n{r['summary']}" for r in rows]
        yield event.plain_result("\n\n".join(out))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("说说", alias={"看看说说"})
    async def show_moments(self, event: AstrMessageEvent):
        """用法：/说说 看最近几条稿
                /说说 写 立刻写一条（写完仍在库里，发不发由你定）"""
        raw = (event.message_str or "").strip()
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if args == "写":
            provider = self.context.get_using_provider()
            if provider is None:
                yield event.plain_result("没有可用 provider")
                return
            try:
                m = await moments.ensure_from_diary(provider, self.context)
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(f"写不出来：{e}")
                return
            work.log("把日记备成了说说稿", "manual")
            pic = f"\n配图：{os.path.basename(m['image_path'])}" if m.get("image_path") else "\n（这次没挑到合适的图）"
            yield event.plain_result(f"#{m['id']}（{m['day']}）\n{m['text']}{pic}")
            return

        if args.startswith("发"):
            mid = args.split(" ", 1)[1].strip() if " " in args else ""
            if not mid.isdigit():
                yield event.plain_result("用法：/说说 发 <编号>")
                return
            row = store.get_moment(int(mid))
            if not row:
                yield event.plain_result(f"没有 #{mid} 这条稿")
                return
            # 转发到群的那句话也由她来说，不用固定句
            note = await voice.say(self.context.get_using_provider(), "forward",
                                   {"内容": "今天的日记（配了张小屋的图）"})
            did = moments.to_space_draft(row["text"], row.get("image_path") or "", note,
                                         self.config.get("moment_forward_target", ""))
            if not did:
                yield event.plain_result("没能送进草稿箱（space_kit 库不在？）")
                return
            work.log("把说说稿送进了发布箱", "manual")
            yield event.plain_result(f"已送进发布箱：#{did}\n确认发布用 /动态发 {did}")
            return

        rows = store.recent_moments(5)
        if not rows:
            yield event.plain_result("还没有说说稿。/说说 写 把今天的日记备成一条。")
            return
        out = [f"#{r['id']} {r['day']}（{r['mood'] or '—'}｜{r['status']}）\n{r['text']}" for r in rows]
        yield event.plain_result("\n\n".join(out))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("文档", alias={"取文档"})
    async def send_doc(self, event: AstrMessageEvent):
        """把项目文档分段发到当前会话。

        QQ 适配器在别的进程里，读不到这台机器的路径，所以文件直发会 ENOENT。
        改成分段贴文本——虽然笨，但一定到得了。
        用法：/文档            列出可取的文档
              /文档 指令清单    发这一份（长文按 2500 字切段）
        """
        import os

        # 这里原来是写死的 /AstrBot/maintenance（本机私有笔记目录）。
        # 分享出去的人没有这个目录，命令就永远回"没有这份文档"。
        # 现在默认还是老路径，但可以用配置 docs_dir 指到自己的笔记目录。
        base = str(self.config.get("docs_dir") or "/AstrBot/maintenance").rstrip("/")
        docs = {
            "指令清单": f"{base}/指令清单.md",
            "功能总览": f"{base}/大肥鱼功能总览.md",
            "交接": f"{base}/projects/memory-life/交接-20261026.md",
            "人格卡": f"{base}/projects/memory-life/人格卡.md",
        }
        if not os.path.isdir(base):
            yield event.plain_result(
                f"文档目录不存在：{base}\n"
                "在插件配置里把「文档目录」指到你的笔记目录，或者忽略这条命令。")
            return
        raw = (event.message_str or "").strip()
        key = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not key:
            yield event.plain_result("可取：" + "、".join(docs) + "\n用法：/文档 指令清单")
            return
        path = docs.get(key) or next((v for k, v in docs.items() if key in k), "")
        if not path or not os.path.exists(path):
            yield event.plain_result(f"没有这份文档：{key}")
            return
        text = open(path, encoding="utf-8").read()
        chunk = 2500
        parts = [text[i:i + chunk] for i in range(0, len(text), chunk)] or [""]
        for i, part in enumerate(parts, 1):
            yield event.plain_result(f"《{os.path.basename(path)}》{i}/{len(parts)}\n{part}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("晚报", alias={"睡前说一句"})
    async def evening_report(self, event: AstrMessageEvent):
        """预览今晚的晚报；加"发"就真的发到配置的那个私聊。"""
        raw = (event.message_str or "").strip()
        do_send = raw.split(" ", 1)[1].strip() == "发" if " " in raw else False
        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用 provider")
            return
        target = str(self.config.get("evening_report_target") or event.get_sender_id())
        text = await voice.say(provider, "evening", self._evening_info(), session=target)
        if not text:
            yield event.plain_result("没写出来（空、太长或命中禁忌）")
            return
        if not do_send:
            yield event.plain_result(f"（预览，未发送）\n{text}")
            return
        await self._send(target, MessageChain([Plain(text)]))
        store.mark_push(store.today_local(), "evening")
        work.log("发了今晚的晚报", "scheduled")
        yield event.plain_result(f"已发：{text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发日记", alias={"日记发"})
    async def quick_diary(self, event: AstrMessageEvent):
        """两步走：/发日记 看稿，/发日记 发 送到发布箱。

        之前要写、审、再过、再发，四五条指令才走完；现在预览和确认各一条。
        内部状态机没变（draft → approved → used），只是不再需要你手动推着它走。
        """
        raw = (event.message_str or "").strip()
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""

        if args in ("发", "发送", "确认"):
            rows = [r for r in store.recent_moments(8) if r.get("status") == "draft"]
            if not rows:
                yield event.plain_result("没有待发的稿子。先 /发日记 看一篇。")
                return
            row = rows[0]
            moments.approve(row["id"])
            provider = self.context.get_using_provider()
            note = await voice.say(provider, "forward", {"内容": "今天的日记（配了张小屋的图）"})
            did = moments.to_space_draft(row["text"], row.get("image_path") or "", note,
                                         self.config.get("moment_forward_target", ""))
            if not did:
                yield event.plain_result("没能送进发布箱（发布插件不在？）稿子还留着。")
                return
            store.set_moment_status(row["id"], "used")
            work.log("把稿子交给了发布插件", "manual")
            yield event.plain_result(f"已经放进发布箱：#{did}\n最后一步：/动态发 {did}")
            return

        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用 provider")
            return
        try:
            m = await moments.ensure_from_diary(provider, self.context)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"写不出来：{e}")
            return
        work.log("写了今天的日记，配了张图", "manual", actor="self")
        head = f"#{m['id']} {m['day']}"
        tail = "满意就 /发日记 发"
        yield event.plain_result(f"{head}\n\n{m['text']}\n\n{tail}")
        if m.get("image_path"):
            yield event.image_result(m["image_path"])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("写日记", alias={"记今天"})
    async def write_diary(self, event: AstrMessageEvent):
        """今天写一篇，顺手配上图，生成待审的稿。"""
        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用 provider")
            return
        try:
            m = await moments.ensure_from_diary(provider, self.context)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"写不出来：{e}")
            return
        work.log("写了今天的日记，配了张图", "manual", actor="self")
        pic = f"配图：{m['scene'] or '无'}" if m.get("image_path") else "配图：没画出来"
        yield event.plain_result(
            f"#{m['id']} {m['day']}\n{m['text']}\n{pic}\n再审：/审日记 过 {m['id']}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("审日记", alias={"待审"})
    async def review_diary(self, event: AstrMessageEvent):
        """用法：/审日记            列出待审的稿
                /审日记 过 <编号>   这篇可以发
        写、审在我们这边；配图调生图插件；发布交给发布插件。"""
        raw = (event.message_str or "").strip()
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""

        if args.startswith("过"):
            mid = args.split(" ", 1)[1].strip() if " " in args else ""
            if not mid.isdigit():
                yield event.plain_result("用法：/审日记 过 <编号>")
                return
            if not moments.approve(int(mid)):
                yield event.plain_result(f"没有 #{mid} 这条稿")
                return
            work.log("审过一篇日记稿", "manual")
            yield event.plain_result(f"#{mid} 审过了。/发布 {mid} 交给发布插件")
            return

        rows = [r for r in store.recent_moments(8) if r.get("status") == "draft"]
        if not rows:
            yield event.plain_result("没有待审的稿。/日记 写 会生成一篇并配上图。")
            return
        out = []
        for r in rows:
            pic = "有图" if r.get("image_path") else "无图"
            out.append(f"#{r['id']} {r['day']}（{r['mood'] or '—'}｜{pic}）\n{r['text']}")
        out.append("看好了就 /审日记 过 <编号>")
        yield event.plain_result("\n\n".join(out))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("测配图", alias={"配图测试"})
    async def test_scene_image(self, event: AstrMessageEvent):
        """强制生成配图，看看小屋长什么样。

        用法：/测配图          四个时段各生成一张
              /测配图 dusk     只生成傍晚那张
        生成的是正式提示词（含心情修饰），会进生图插件的任务库。
        """
        raw = (event.message_str or "").strip()
        arg = raw.split(" ", 1)[1].strip() if " " in raw else ""
        parts = arg.split()
        key = parts[0] if parts and parts[0] in scenes.SCENES else ""
        variant = None
        if len(parts) > 1 and parts[1].isdigit():
            variant = int(parts[1])          # 指定变体，方便对比同一时段的三张
        keys = [key] if key else list(scenes.SCENES)
        yield event.plain_result(f"开始生成 {len(keys)} 张（{'、'.join(keys)}），每张十几秒……")
        for k in keys:
            _, prompt = scenes.build_prompt(force_key=k, variant=variant)
            image, scene = await scenes.make(self.context, force_key=k, variant=variant)
            if not image:
                yield event.plain_result(f"{k}：没画出来（生图插件未就绪或返回空）")
                continue
            yield event.plain_result(f"{k} 好了\n提示词：{prompt}\n文件：{os.path.basename(image)}")
            yield event.image_result(image)
        work.log("试画了几张小屋的配图", "manual")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("配图")
    async def remake_image(self, event: AstrMessageEvent):
        """用法：/配图 <编号>  重画一张（走生图插件的通用接口）"""
        raw = (event.message_str or "").strip()
        mid = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not mid.isdigit():
            yield event.plain_result("用法：/配图 <编号>")
            return
        image, scene = await moments.remake_image(self.context, int(mid))
        if not image:
            yield event.plain_result("没画出来（生图插件未就绪或返回空）")
            return
        work.log("给稿子重画了张配图", "manual")
        yield event.plain_result(f"#{mid} 换好了：{scene}\n{os.path.basename(image)}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("发布")
    async def publish_moment(self, event: AstrMessageEvent):
        """用法：/发布 <编号>  只送进发布插件的草稿箱，最后一步仍由你确认"""
        raw = (event.message_str or "").strip()
        mid = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if not mid.isdigit():
            yield event.plain_result("用法：/发布 <编号>")
            return
        row = store.get_moment(int(mid))
        if not row:
            yield event.plain_result(f"没有 #{mid} 这条稿")
            return
        if row.get("status") != "approved":
            yield event.plain_result("还没审过。/审日记 过 <编号> 先审一遍")
            return
        # 转发到群的那句话也由她来说，不用固定句
        note = await voice.say(self.context.get_using_provider(), "forward",
                               {"内容": "今天的日记（配了张小屋的图）"})
        did = moments.to_space_draft(row["text"], row.get("image_path") or "", note,
                                     self.config.get("moment_forward_target", ""))
        if not did:
            yield event.plain_result("没能送进发布箱（发布插件不在？）")
            return
        store.set_moment_status(int(mid), "used")
        work.log("把稿子交给了发布插件", "manual")
        yield event.plain_result(f"已送进发布箱：#{did}\n确认发布用 /动态发 {did}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日记", alias={"看看日记"})
    async def show_diary(self, event: AstrMessageEvent):
        """用法：/日记          看最近几篇
                /日记 写       给今天补一篇（昨天那种由每日任务自动补）"""
        raw = (event.message_str or "").strip()
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""
        if args == "写":
            provider = self.context.get_using_provider()
            if provider is None:
                yield event.plain_result("没有可用 provider")
                return
            try:
                got = await diary.generate(provider)
            except Exception as e:  # noqa: BLE001
                yield event.plain_result(f"写不出来：{e}")
                return
            yield event.plain_result(f"{got['day']}（{got['mood']}）\n{got['text']}")
            return

        rows = store.recent_diaries(5)
        if not rows:
            yield event.plain_result("还没有日记。第一次会在明天醒来后自动补上。")
            return
        out = []
        for r in rows:
            out.append(f"{r['day']}（{r['mood'] or '—'}）\n{r['text']}")
        yield event.plain_result("\n\n".join(out))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("熟人")
    async def show_relations(self, event: AstrMessageEvent):
        """看她心里谁近谁远。私聊主动就从这里挑人。"""
        rows = store.relations(min_relation=0, limit=10)
        if not rows:
            yield event.plain_result("还没有熟人记录（多聊几句就会有了）")
            return
        lines = ["好感度 / 最近出现"]
        for p in rows:
            name = ""
            try:
                import json as _json
                names = _json.loads(p.get("names") or "{}")
                name = next(iter(names.values()), "")
            except Exception:  # noqa: BLE001
                pass
            lines.append(f"{p.get('relation', 0):>3}  {relation.describe(int(p.get('relation') or 0))}  {p['uid']} {name}  {str(p.get('last_seen'))[:16]}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("说一句")
    async def preview_speak(self, event: AstrMessageEvent):
        """看她会主动说什么。默认只预览；加"发"才真的发出去。"""
        raw = (event.message_str or "").strip()
        args = raw.split(" ", 1)[1].strip() if " " in raw else ""
        do_send = args == "发"
        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用 provider")
            return
        sid = event.get_session_id()

        # 可选：给一个时间，用那个时刻的场景去试（比如白天有活动块的时候）
        when = None
        m = re.match(r"^(\d{1,2}):(\d{2})", args)
        if m:
            when = datetime.datetime.now(store.TZ).replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        try:
            text = await speak.compose(provider, sid, when)
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"生成失败：{e}")
            return
        if not text:
            yield event.plain_result("没写出合适的（空、太长，或像客服腔被拦了）")
            return
        if not do_send:
            yield event.plain_result(f"（预览，未发送）{text}")
            return
        await self._send(sid, MessageChain([Plain(text)]))
        speak.remember_spoken(sid, text, 0.0)
        yield event.plain_result(f"已发出：{text}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("抽一次", alias={"抽取测试"})
    async def try_extract(self, event: AstrMessageEvent):
        """用法：/抽一次 他说明天要去医院复查，语气有点闷"""
        raw = (event.message_str or "").strip()
        text = raw.split(" ", 1)[1] if " " in raw else ""
        if not text:
            yield event.plain_result("用法：/抽一次 <一段用户说的话>")
            return
        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用 provider")
            return
        try:
            person, events = await extract.extract(provider, text, "")
        except Exception as e:  # noqa: BLE001
            yield event.plain_result(f"抽取失败：{e}")
            return
        if not events and not person.get("name") and not person.get("impression"):
            yield event.plain_result("这段没有值得记的东西（也可能被校验丢了）")
            return
        n = extract.save(event.get_sender_id(), event.get_session_id(), person, events,
                         sender_name=self._sender_name(event))
        work.log("从一段话里记了件事", "manual")
        lines = [f"已记 {n} 条｜人物：{person.get('name') or '—'}｜{person.get('impression') or '—'}"]
        for ev in events:
            lines.append(f"[{ev['importance']}] {ev['kind']}·{ev['sentiment']} {ev['summary']}"
                         f"（{ev['keywords']}）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("闸门", alias={"能不能开口"})
    async def show_gate(self, event: AstrMessageEvent):
        enabled = bool(self.config.get("proactive_enabled", False))
        yield event.plain_result(gate.explain(event.get_session_id(), enabled))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def watch_incoming(self, event: AstrMessageEvent):
        """记群里的来话（一分钟最多一次）。给两件事用：群还活着吗、她说的有人接吗。"""
        try:
            sid = event.get_session_id()
            self._learn_umo(event)
            now = datetime.datetime.now(store.TZ)
            last = store.last_incoming(sid)
            if not last or (now - store.to_local(last["ts"])).total_seconds() > 60:
                umo = getattr(event, "unified_msg_origin", "") or ""
                store.record_message(sid, event.get_sender_id(),
                                     is_group=":GroupMessage:" in umo)
                store.mark_proactive_replied(sid, str(event.get_sender_id()))
                if event.get_sender_id() != event.get_self_id():
                    store.bump_relation(event.get_sender_id(), 1)   # 聊一次，近一点
        except Exception as e:  # noqa: BLE001
            LOG.debug(f"[life_memory] 记录来话失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("主动意愿", alias={"想不想说话"})
    async def show_proactivity(self, event: AstrMessageEvent):
        """看此刻她有没有可能主动开口，以及凭什么这么算。不发消息，只看判断。"""
        yield event.plain_result(proactivity.explain())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("今天")
    async def show_today(self, event: AstrMessageEvent):
        loaded = dayplan.load_day(store.today_local())
        yield event.plain_result(dayplan.render_plan(loaded))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("补一天", alias={"重排今天"})
    async def regen_today(self, event: AstrMessageEvent):
        """强制重生成今天的计划。校验不过会说清原因，不会写坏数据。"""
        provider = self.context.get_using_provider()
        if provider is None:
            yield event.plain_result("没有可用的 provider")
            return
        yield event.plain_result("正在生成，可能要十几秒……")
        got = await dayplan.ensure_day(provider, force=True)
        if not got:
            work.log("重排今天的安排", "manual", status="fail", note=dayplan.LAST_ERROR[:80])
            LOG.warning(f"[life_memory] 重排失败：{dayplan.LAST_ERROR}")
            yield event.plain_result(
                f"生成失败：{dayplan.LAST_ERROR or '未知原因'}\n（今天沿用旧计划或没有计划）"
            )
            return
        yield event.plain_result(dayplan.render_plan(got))

    # ---------- 调试用指令（管理） ----------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("记忆库")
    async def show_stats(self, event: AstrMessageEvent):
        with store.connect() as conn:
            ev = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
            pe = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
            bl = conn.execute("SELECT COUNT(*) FROM life_block").fetchone()[0]
        yield event.plain_result(
            f"记忆库：{store.db_path()}\n事件 {ev}｜人物 {pe}｜活动块 {bl}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("记一笔")
    async def add_note(self, event: AstrMessageEvent):
        """用法：!记一笔 3 他上周末说在准备考试，语气挺紧绷的

        第一个参数是重要度 1-5，剩下的是摘要。用来手工造数据验收 retrieve。
        """
        raw = (event.message_str or "").strip()
        raw = raw.split(" ", 1)[1] if " " in raw else ""
        parts = raw.split(" ", 1)
        if len(parts) < 2 or not parts[0].isdigit():
            yield event.plain_result("用法：!记一笔 <重要度1-5> <一句话摘要>")
            return
        importance, summary = int(parts[0]), parts[1].strip()
        uid = event.get_sender_id()
        sid = event.get_session_id()
        store.upsert_person(uid)
        eid = store.add_event(uid, sid, "info", summary, importance=importance)
        yield event.plain_result(f"已记 #{eid}（重要度 {importance}）：{summary}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("记忆查询")
    async def query(self, event: AstrMessageEvent):
        """用法：!记忆查询 考试 —— 看 retrieve 会挑出什么、怎么措辞。"""
        raw = (event.message_str or "").strip()
        scene = raw.split(" ", 1)[1] if " " in raw else ""
        uid = event.get_sender_id()
        res = recall.retrieve(uid, scene)
        if not res["items"]:
            yield event.plain_result("（没想起什么）")
            return
        store.log_use(uid, event.get_session_id(), res["ids"])
        lines = [f"场景：{scene or '（空）'}", f"命中 {len(res['items'])} 条｜{len(res['text'])} 字", "", res["text"]]
        yield event.plain_result("\n".join(lines))

    # ---------- WebUI 页面接口 ----------
    # 页面在 pages/app/index.html，通过 window.AstrBotPluginPage.apiGet/apiPost 调这些路由。

    def _register_web_apis(self) -> None:
        routes = [
            ("overview", self._api_overview, ["GET"], "总览：她此刻的状态与今天的安排"),
            ("memory", self._api_memory, ["GET"], "记忆库与人物"),
            ("logs", self._api_logs, ["GET"], "发言与主动记录"),
            ("work", self._api_work, ["GET"], "她和分身做过的活"),
            ("memory_del", self._api_memory_del, ["POST"], "删一条记忆"),
            ("memory_edit", self._api_memory_edit, ["POST"], "改一条记忆（摘要/重要度等）"),
            ("replan", self._api_replan, ["POST"], "重排今天的计划"),
            ("preview", self._api_preview, ["POST"], "预览一句主动发言"),
            ("moment", self._api_moment, ["POST"], "生成或读取说说稿"),
            ("config", self._api_config, ["POST"], "修改开关"),
            ("theme_get", self._api_theme_get, ["GET"], "读外观主题"),
            ("theme_set", self._api_theme_set, ["POST"], "保存外观主题"),
        ]
        for route, handler, methods, desc in routes:
            self.context.register_web_api(f"/{PLUGIN_NAME}/{route}", handler, methods, desc)

    # ---------- 外观：主题与背景 ----------

    def _theme_path(self) -> str:
        return os.path.join(paths.plugin_dir(), "theme.json")

    def _read_theme(self) -> dict:
        try:
            with open(self._theme_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _api_theme_get(self):
        return jsonify({"ok": True, "theme": self._read_theme()})

    async def _api_theme_set(self):
        """保存外观。body: {preset?, colors?, bg?, bg_dim?}

        背景图走 data URL（前端先压到 1600px/JPEG），不落地二进制文件，
        省掉一个文件服务路由，也不用担心路径穿越。
        """
        body = await request.get_json(silent=True) or {}
        theme = self._read_theme()

        if body.get("reset"):
            theme = {}
        else:
            preset = str(body.get("preset") or "").strip()[:24]
            if preset:
                theme["preset"] = preset
            colors = body.get("colors")
            if isinstance(colors, dict):
                clean = {}
                for key, value in colors.items():
                    value = str(value or "").strip()
                    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
                        clean[str(key)[:16]] = value
                theme["colors"] = clean
            if "bg" in body:
                bg = str(body.get("bg") or "")
                if bg and not bg.startswith("data:image/"):
                    return jsonify({"ok": False, "error": "背景要 data:image 开头"})
                if len(bg) > 3_500_000:
                    return jsonify({"ok": False, "error": "图片太大（压到 2MB 以内）"})
                theme["bg"] = bg
            if "bg_dim" in body:
                try:
                    theme["bg_dim"] = max(0.0, min(0.9, float(body["bg_dim"])))
                except (TypeError, ValueError):
                    pass

        try:
            os.makedirs(os.path.dirname(self._theme_path()), exist_ok=True)
            with open(self._theme_path(), "w", encoding="utf-8") as f:
                json.dump(theme, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"写盘失败：{exc}"})
        return jsonify({"ok": True, "theme": theme})

    def _persona_len(self) -> int:
        try:
            import sqlite3
            c = sqlite3.connect(paths.persona_db())
            row = c.execute("select length(system_prompt) from personas where persona_id='default'").fetchone()
            c.close()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    async def _api_overview(self):
        today = store.today_local()
        now = datetime.datetime.now(store.TZ)
        loaded = dayplan.load_day(today)
        blk = store.current_block(today, now.strftime("%H:%M"))
        p = proactivity.score(now)

        group_gate = gate.can_speak("", bool(self.config.get("proactive_enabled", False)), now)
        rows = store.relations(min_relation=0, limit=5)
        people = [{"uid": r["uid"], "relation": int(r.get("relation") or 0),
                   "desc": relation.describe(int(r.get("relation") or 0))} for r in rows]

        return jsonify({
            "now": now.strftime("%m-%d %H:%M"),
            "asleep": state.is_asleep(now),
            "persona_len": self._persona_len(),
            "today": today,
            "mood": (loaded or {}).get("mood", ""),
            "blocks": (loaded or {}).get("blocks", []),
            "current": blk,
            "willingness": p["score"],
            "verdict": p["verdict"],
            "reasons": p["reasons"],
            "gate_group": group_gate,
            "gate_private_ok": bool(people) and gate.can_speak_private(
                people[0]["uid"], bool(self.config.get("proactive_private_enabled", False)),
                people[0]["relation"], now)["ok"],
            "people": people,
            "config": {
                "extract_enabled": bool(self.config.get("extract_enabled", False)),
                "extract_sessions": self.config.get("extract_sessions", []),
                "extract_hourly_limit": int(self.config.get("extract_hourly_limit", 10)),
                "proactive_enabled": bool(self.config.get("proactive_enabled", False)),
                "proactive_sessions": self.config.get("proactive_sessions", []),
                "proactive_private_enabled": bool(self.config.get("proactive_private_enabled", False)),
            },
            "counts": {
                "event": self._count("event"),
                "person": self._count("person"),
                "speech": self._count("speech_log"),
                "proactive": self._count("proactive_log"),
                "wake": self._count("wake_log"),
                "message": self._count("message_log"),
                "work": self._count("work_log"),
            },
        })

    def _count(self, table: str) -> int:
        try:
            with store.connect() as conn:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0

    def _display_names(self) -> dict[str, str]:
        """uid → 显示名。别名合并后取像人名的那个；重名加 uid 后缀区分。"""
        out: dict[str, str] = {}
        merged: dict[str, list[str]] = {}
        raw_rows = []
        try:
            with store.connect() as conn:
                raw_rows = conn.execute("SELECT uid, names FROM person").fetchall()
        except Exception:  # noqa: BLE001
            return out

        for r in raw_rows:
            uid = str(r["uid"] or "")
            if not uid:
                continue
            try:
                names = json.loads(r["names"] or "{}")
            except Exception:  # noqa: BLE001
                names = {}
            bucket = merged.setdefault(store.canon(uid), [])
            for key, value in names.items():
                value = str(value).strip()
                if not value:
                    continue
                # 群昵称（key 是群号）比网页端登录名更像人
                if str(key).startswith("webchat"):
                    bucket.append(value)
                else:
                    bucket.insert(0, value)

        for canon_uid, vals in merged.items():
            uniq: list[str] = []
            for v in vals:
                if v not in uniq:
                    uniq.append(v)
            # 有中文名就别用登录名
            cjk = [v for v in uniq if any(ord(ch) > 127 for ch in v)]
            out[canon_uid] = (cjk or uniq or [canon_uid])[0]

        # 同名不同人：主人保留原名，其他人挂 uid 后四位
        owners = set()
        try:
            owners = {store.canon(str(a)) for a in (self.context.get_config().get("admins_id") or [])}
        except Exception:  # noqa: BLE001
            owners = set()
        counts: dict[str, int] = {}
        for name in out.values():
            counts[name] = counts.get(name, 0) + 1
        for uid, name in list(out.items()):
            if counts.get(name, 0) > 1 and uid not in owners:
                out[uid] = f"{name} #{str(uid)[-4:]}"

        # 人物表里没昵称的，从平台消息历史补一个最近用过的
        missing = [u for u, n in out.items() if n == u]
        if missing:
            try:
                _db = os.path.join(paths.data_root(), "data_v4.db")
                _conn = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
                for uid in missing:
                    row = _conn.execute(
                        "SELECT sender_name FROM platform_message_history "
                        "WHERE sender_id=? AND sender_name<>'' AND sender_name<>'bot' "
                        "ORDER BY id DESC LIMIT 1", (uid,)
                    ).fetchone()
                    if not row:
                        continue
                    name = str(row[0] or "").strip()
                    # 过滤系统提示类的假昵称，比如「你已成为群主，快去管理群聊吧」
                    if not name or len(name) > 16:
                        continue
                    if any(bad in name for bad in ("已成为", "管理群聊", "系统消息", "申请加入")):
                        continue
                    out[uid] = name
                _conn.close()
            except Exception:  # noqa: BLE001
                pass

        # 别名 uid 也指向同一个名字（事件里可能存的是别名）
        for r in raw_rows:
            uid = str(r["uid"] or "")
            if uid and uid not in out:
                out[uid] = out.get(store.canon(uid), uid)
        self._name_cache = (time.time(), dict(out))
        return out

    def _relations(self) -> dict[str, int]:
        """uid → 好感度。一次取完，别每条记忆查一次库。"""
        out: dict[str, int] = {}
        try:
            with store.connect() as conn:
                for r in conn.execute("SELECT uid, relation FROM person"):
                    out[str(r["uid"] or "")] = int(r["relation"] or 0)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _name_of(self, uid: str, names: dict[str, str] | None = None,
                 rels: dict[str, int] | None = None) -> tuple[str, int]:
        """返回 (显示名, 好感度)。uid 可能是别名，过一遍 canon。"""
        uid = str(uid or "")
        if not uid:
            return "不确定是谁", 0
        names = names if names is not None else self._display_names()
        rels = rels if rels is not None else self._relations()
        canon_uid = store.canon(uid)
        name = names.get(uid) or names.get(canon_uid) or canon_uid or uid
        return name, int(rels.get(canon_uid, rels.get(uid, 0)) or 0)

    async def _api_memory(self):
        """支持 q / sentiment / min_importance / offset / limit。

        记忆会越来越多，界面必须能筛能翻，否则三百条之后就是一面墙。
        """
        from quart import request as _req

        q = (_req.args.get("q") or "").strip()
        sentiment = _req.args.get("sentiment") or ""
        want_uid = (_req.args.get("uid") or "").strip()
        try:
            min_imp = int(_req.args.get("min_importance") or 0)
            offset = max(0, int(_req.args.get("offset") or 0))
            limit = min(1000, max(1, int(_req.args.get("limit") or 40)))
        except (TypeError, ValueError):
            min_imp, offset, limit = 0, 0, 40

        # 2026-10-28：不筛选时不能拿"最近 N 条"当分页。
        # 最近 40 条可能全是一个人说的，界面按 uid 分组以后就只剩 3 个人，
        # 看着像"记忆丢了"。没有筛选条件时一律给全量（上限 1000）。
        if not q and not sentiment and min_imp <= 0 and not want_uid:
            limit = 1000

        events, total = store.search_events(q, sentiment, min_imp, offset, limit, uid=want_uid)
        names = self._display_names()
        rels = self._relations()
        for ev in events:
            ev["when"] = store.to_local(ev["ts"]).strftime("%m-%d %H:%M")
            ev["name"], ev["relation"] = self._name_of(ev.get("uid") or "", names, rels)

        # 今日面板仍取最近 12 条（不受筛选影响），方便"今天记住了什么"一眼看完
        with store.connect() as conn:
            today_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM event ORDER BY ts DESC LIMIT 12")]
            persons = [dict(r) for r in conn.execute(
                "SELECT * FROM person ORDER BY relation DESC, last_seen DESC LIMIT 50")]
        for ev in today_rows:
            ev["when"] = store.to_local(ev["ts"]).strftime("%m-%d %H:%M")
        for p in persons:
            p["desc"] = relation.describe(int(p.get("relation") or 0))

        return jsonify({"events": events, "total": total, "offset": offset,
                        "limit": limit, "today": today_rows, "persons": persons,
                        "names": names, "uid": want_uid})

    async def _api_memory_edit(self):
        """改一条记忆。body: {id, summary?, importance?, kind?, sentiment?, keywords?}

        2026-10-28 新增：抽取难免把话说歪，原先只能删掉重来，改不了。
        只允许动这几个字段，uid/ts 这类归属与时间不让改。
        """
        body = await request.get_json(silent=True) or {}
        try:
            eid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "id 不对"})
        if not eid:
            return jsonify({"ok": False, "error": "缺少 id"})

        fields: dict = {}
        summary = body.get("summary")
        if summary is not None:
            summary = str(summary).strip()
            if not summary:
                return jsonify({"ok": False, "error": "摘要不能为空"})
            if len(summary) > 500:
                summary = summary[:500]
            fields["summary"] = summary
        for key, low, high in (("importance", 1, 5),):
            if body.get(key) is not None:
                try:
                    fields[key] = max(low, min(high, int(body[key])))
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": key + " 要数字"})
        for key in ("kind", "sentiment", "keywords"):
            if body.get(key) is not None:
                value = str(body[key]).strip()
                if value:
                    fields[key] = value

        if not fields:
            return jsonify({"ok": False, "error": "没有要改的字段"})
        ok = store.update_event(eid, fields)
        if ok:
            work.log("改了一条记忆", "manual")
        return jsonify({"ok": ok, "id": eid, "fields": fields})

    async def _api_memory_del(self):
        """body: {"id": n}。删掉就真忘掉，不是标记。"""
        body = await request.get_json(silent=True) or {}
        try:
            eid = int(body.get("id") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "id 不对"})
        ok = store.delete_event(eid)
        if ok:
            work.log("让分身忘掉一件事", "manual")
        return jsonify({"ok": ok, "id": eid})

    async def _api_logs(self):
        with store.connect() as conn:
            proactive = [dict(r) for r in conn.execute(
                "SELECT * FROM proactive_log ORDER BY ts DESC LIMIT 40")]
            speech = [dict(r) for r in conn.execute(
                "SELECT * FROM speech_log ORDER BY ts DESC LIMIT 40")]
            early_pro = [dict(r) for r in conn.execute(
                "SELECT * FROM speech_log WHERE kind='proactive' ORDER BY ts DESC LIMIT 80")]

        names = self._display_names()
        for r in proactive + speech:
            r["when"] = store.to_local(r["ts"]).strftime("%m-%d %H:%M")

        # 早期记录没存原话：从 speech_log 里找同一会话、时间最近的那条主动发言补上
        pool: dict[str, list[dict]] = {}
        for item in early_pro:
            if item.get("kind") == "proactive":
                pool.setdefault(str(item.get("session")), []).append(item)
        for r in proactive:
            if r.get("text"):
                continue
            best_text, best_gap = "", None
            for cand in pool.get(str(r.get("session")), []):
                try:
                    gap = abs((store.to_local(cand["ts"]) - store.to_local(r["ts"])).total_seconds())
                except Exception:  # noqa: BLE001
                    continue
                if best_gap is None or gap < best_gap:
                    best_text, best_gap = cand.get("text") or "", gap
            if best_text and (best_gap or 0) <= 180:
                r["text"] = best_text

        for r in proactive:
            uid = str(r.get("replied_uid") or "")
            r["replied_name"] = (names.get(uid) or names.get(store.canon(uid)) or uid) if uid else ""
            r["replied_delay"] = None
            if r.get("replied_at") and r.get("ts"):
                try:
                    delta = (store.to_local(r["replied_at"]) - store.to_local(r["ts"])).total_seconds()
                    r["replied_delay"] = max(0, int(delta // 60))
                except Exception:  # noqa: BLE001
                    pass
        LOG.info(f"[life_memory] logs 接口：主动 {len(proactive)} 条，发言 {len(speech)} 条")
        return jsonify({"proactive": proactive, "speech": speech})

    async def _api_replan(self):
        provider = self.context.get_using_provider()
        if provider is None:
            return jsonify({"ok": False, "error": "没有可用 provider"})
        got = await dayplan.ensure_day(provider, force=True)
        if not got:
            return jsonify({"ok": False, "error": dayplan.LAST_ERROR or "生成失败"})
        return jsonify({"ok": True, "mood": got.get("mood", ""), "blocks": got["blocks"]})

    async def _api_preview(self):
        body = await request.get_json(silent=True) or {}
        provider = self.context.get_using_provider()
        if provider is None:
            return jsonify({"ok": False, "error": "没有可用 provider"})
        when = None
        hm = str(body.get("time") or "")
        if re.match(r"^\d{1,2}:\d{2}$", hm):
            h, m = hm.split(":")
            when = datetime.datetime.now(store.TZ).replace(
                hour=int(h), minute=int(m), second=0, microsecond=0)
        uid = str(body.get("uid") or "")
        if not uid:
            top = store.relations(min_relation=0, limit=1)
            uid = top[0]["uid"] if top else ""
        try:
            text = await speak.compose(provider, uid, when)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return jsonify({"ok": bool(text), "text": text})


    async def _api_work(self):
        rows = store.recent_work(20)
        for r in rows:
            r["when"] = store.to_local(r["ts"]).strftime("%m-%d %H:%M")
        return jsonify({"ok": True, "items": rows})

    async def _api_moment(self):
        """body: {"action": "list"|"gen"|"approve"|"image"|"publish", "id": n}"""
        body = await request.get_json(silent=True) or {}
        action = str(body.get("action") or "list")
        mid = body.get("id")
        provider = self.context.get_using_provider()

        if action == "gen":
            if provider is None:
                return jsonify({"ok": False, "error": "没有可用 provider"})
            try:
                m = await moments.ensure_from_diary(provider, self.context)
            except Exception as e:  # noqa: BLE001
                return jsonify({"ok": False, "error": str(e)})
            return jsonify({"ok": True, "id": m["id"], "text": m["text"]})

        if action in ("approve", "image", "publish", "reject", "delete"):
            if not isinstance(mid, int) and not str(mid or "").isdigit():
                return jsonify({"ok": False, "error": "缺少编号"})
            mid = int(mid)
            row = store.get_moment(mid)
            if not row:
                return jsonify({"ok": False, "error": f"没有 #{mid} 这条稿"})

            if action == "approve":
                moments.approve(mid)
                work.log("审过一篇日记稿", "manual")
                return jsonify({"ok": True, "id": mid, "status": "approved"})

            if action == "reject":
                store.set_moment_status(mid, "rejected")
                work.log("否掉了一篇日记稿", "manual")
                return jsonify({"ok": True, "id": mid, "status": "rejected"})

            if action == "delete":
                img = row.get("image_path") or ""
                ok = store.delete_moment(mid)
                if ok and img and os.path.exists(img) and "/moments/" in img:
                    try:
                        os.remove(img)      # 稿子删了，配图也没用了（只在自家目录下删）
                    except OSError:
                        pass
                work.log(("删掉" if ok else "想删但没找到") + "一篇日记稿", "manual")
                return jsonify({"ok": ok, "id": mid, "deleted": ok})

            if action == "image":
                image, scene = await moments.remake_image(self.context, mid)
                if not image:
                    return jsonify({"ok": False, "error": "生图没返回路径"})
                work.log("给稿子重画了张配图", "manual")
                return jsonify({"ok": True, "id": mid, "image": os.path.basename(image), "scene": scene})

            # publish
            if row.get("status") != "approved":
                return jsonify({"ok": False, "error": "还没审过，先点通过"})
            note = await voice.say(provider, "forward", {"内容": "今天的日记（配了张小屋的图）"})
            did = moments.to_space_draft(row["text"], row.get("image_path") or "", note,
                                         self.config.get("moment_forward_target", ""))
            if not did:
                return jsonify({"ok": False, "error": "没能送进发布箱（发布插件不在？）"})
            store.set_moment_status(mid, "used")
            work.log("把稿子交给了发布插件", "manual")
            return jsonify({"ok": True, "id": mid, "draft_id": did, "note": note})

        rows = store.recent_moments(10)
        for r in rows:
            r["when"] = store.to_local(r["created_at"]).strftime("%m-%d %H:%M")
            r["image"] = os.path.basename(r.get("image_path") or "")
        return jsonify({"ok": True, "items": rows})

    async def _api_config(self):
        body = await request.get_json(silent=True) or {}
        allowed = ("extract_enabled", "proactive_enabled", "proactive_private_enabled",
                   "extract_hourly_limit", "extract_sessions", "proactive_sessions")
        for k in allowed:
            if k in body:
                self.config[k] = body[k]
        try:
            import json as _json
            with open(paths.config_path(), "w", encoding="utf-8") as f:
                _json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError as e:
            return jsonify({"ok": False, "error": f"写配置失败：{e}"})
        return jsonify({"ok": True, "config": {k: self.config.get(k) for k in allowed}})
