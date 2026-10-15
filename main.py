import re
import time
from sys import maxsize

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply as CompReply, At as CompAt
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

from .vent import VentWindows, classify_vent, fmt_duration, parse_duration

# 配置缺项时的兜底默认值（与 _conf_schema.json 保持一致）
_DEFAULT_VENT_MINUTES = 5
_DEFAULT_VENT_MAX_MINUTES = 120


class LLMAllowList(Star):
    """框架默认 LLM 回复白名单。

    白名单内用户正常触发 LLM；其余用户在群里被静默跳过（可配置自定义回复）。
    除拦截框架默认 LLM 链路外，还通过 on_llm_request hook 取消一切 LLM 请求，
    因此内置「空 @ 等待」等由 handler 自发的 request_llm 也会被拦住。

    另有「放风」功能：群管理/管理员在群里发「放风5分钟」，这 5 分钟内本群
    **所有用户**的 LLM 对话一律放行（等价于全群临时进白名单），到期自动收回，
    也可以发「收风」提前结束、「放风状态」查看剩余时间。时长解析与窗口表见 vent.py。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._group_admins: dict[str, dict] = {}
        self.vents = VentWindows(
            kv_get=self.get_kv_data,
            kv_put=self.put_kv_data,
            on_expire=self._on_vent_expire,
            logger=logger,
        )

    async def initialize(self) -> None:
        """插件激活时恢复上次未到期的放风窗口（状态存于插件 KV）。"""
        await self.vents.ensure_loaded()

    async def terminate(self) -> None:
        """插件被禁用 / 重载时取消到期提醒任务，放风状态已即时落盘无需另存。"""
        await self.vents.shutdown()

    # ---------------------------------------------------------------- 配置解析
    def _parse_platform_map(self, key):
        raw = self.config.get(key, "")
        if not raw or not raw.strip():
            return {}
        result = {}
        for line in raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r'^(\w+)\[([^\]]*)\]$', line)
            if m:
                platform = m.group(1)
                content = m.group(2).strip()
                result[platform] = content
        return result

    def _parse_platform_sets(self, key):
        """把 `平台名[逗号分隔内容]` 解析成 {平台: {条目, ...}}。"""
        pmap = self._parse_platform_map(key)
        return {p: {u.strip() for u in v.split(",") if u.strip()} for p, v in pmap.items()}

    def _parse_allowlist(self):
        return self._parse_platform_sets("allowlist")

    def _in_allowlist(self, event: AstrMessageEvent) -> bool:
        allowlist = self._parse_allowlist()
        return str(event.get_sender_id()) in allowlist.get(event.get_platform_name(), set())

    def _group_enabled(self, event: AstrMessageEvent) -> bool:
        """本插件是否在该群启用（群名单）。

        群名单留空 = 不限制，所有群都启用（向后兼容）；一旦填写，只有名单内列出的群
        启用本插件，其余群完全不拦截（LLM 正常回复）。

        Args:
            event: 当前消息事件。

        Returns:
            该群启用本插件返回 True。
        """
        groups = self._parse_platform_sets("enable_groups")
        if not groups:
            return True
        gid = str(event.message_obj.group_id or "")
        return gid in groups.get(event.get_platform_name(), set())

    # ---------------------------------------------------------------- 管理员放行
    async def _fetch_group_admins(self, bot, group_id: str):
        try:
            data = await bot.api.call_action("get_group_member_list", group_id=group_id)
            if data:
                admins = {str(m["user_id"]) for m in data if m.get("role") in ("owner", "admin")}
                self._group_admins[group_id] = {"admins": admins, "fetched_at": time.time()}
                logger.debug("[llm_allowlist] admin_fetch | 群%s 管理员%d人: %s", group_id, len(admins), admins)
        except Exception as e:
            logger.debug("[llm_allowlist] admin_fetch | 群%s 获取失败: %s", group_id, e)

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员身份：AstrBot 全局管理员（全平台）+ 群主/群管理员（仅 aiocqhttp）。

        不含 admin_bypass 开关判断，供放风指令等需要「认管理员」的场景共用。

        Args:
            event: 当前消息事件。

        Returns:
            命中任一管理员规则返回 True。
        """
        # 1) AstrBot 全局管理员：event.role 由 WakingCheckStage 按 admins_id 设置，全平台可用
        if event.is_admin():
            logger.debug(
                "[llm_allowlist] admin_check | 全局管理员 sender_id=%s",
                event.get_sender_id(),
            )
            return True

        # 2) 群主/群管理员：依赖 OneBot 的 get_group_member_list，仅 aiocqhttp 有效
        gid = str(event.message_obj.group_id or "")
        evt_bot = getattr(event, "bot", None)
        if not gid or not evt_bot:
            return False
        cache = self._group_admins.get(gid)
        if not cache or time.time() - cache["fetched_at"] > 3600:
            await self._fetch_group_admins(evt_bot, gid)
        admins = self._group_admins.get(gid, {}).get("admins", set())
        logger.debug(
            "[llm_allowlist] admin_check | platform=%s sender_id=%s in_admins=%s admins=%s",
            event.get_platform_name(), event.get_sender_id(),
            str(event.get_sender_id()) in admins, admins,
        )
        return str(event.get_sender_id()) in admins

    async def _is_admin_exempt(self, event: AstrMessageEvent) -> bool:
        """管理员放行：admin_bypass 关闭时不再放行（严格按白名单）。

        Args:
            event: 当前消息事件。

        Returns:
            放行返回 True。
        """
        if not self.config.get("admin_bypass", True):
            return False
        return await self._is_admin(event)

    async def _is_exempt(self, event: AstrMessageEvent) -> bool:
        """白名单内、管理员放行、或本群正在放风。"""
        return (
            self._in_allowlist(event)
            or await self._is_admin_exempt(event)
            or await self._in_vent(event)
        )

    # ---------------------------------------------------------------- 放风
    @staticmethod
    def _vent_key(event: AstrMessageEvent) -> str:
        """放风窗口的键：平台 + 群号（放风只影响发指令的那个群）。"""
        return VentWindows.make_key(
            event.get_platform_name(), event.message_obj.group_id
        )

    async def _in_vent(self, event: AstrMessageEvent) -> bool:
        """当前群是否处于放风窗口内（真 = 全群临时白名单）。

        Args:
            event: 当前消息事件。

        Returns:
            在放风窗口内返回 True。
        """
        if not self.config.get("vent_enabled", True):
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        await self.vents.ensure_loaded()
        return self.vents.is_open(self._vent_key(event))

    async def _on_vent_expire(self, key: str, umo: str):
        """放风到期回调：按配置在群里提醒一句。"""
        if self.config.get("vent_expire_notify", True):
            await self._send_to_session(umo, "⏰ 放风结束，本群恢复白名单。")

    async def _can_control_vent(self, event: AstrMessageEvent) -> bool:
        """谁能放风 / 收风：管理员恒可；白名单用户看 vent_allowlist_can_vent。

        注意这里**不能**用 _is_exempt（它把放风中的所有人也算成放行），否则放风
        期间任何人都能续期。
        """
        if await self._is_admin(event):
            return True
        if not self.config.get("vent_allowlist_can_vent", False):
            return False
        return self._in_allowlist(event)

    async def _handle_vent_command(self, event: AstrMessageEvent) -> bool:
        """识别并处理放风指令（放风X / 收风 / 放风状态）。

        Returns:
            True 表示这条消息是放风指令且已处理完（调用方应直接 return，默认
            LLM 链路已在内部用 should_call_llm(True) 阻断）；False 表示不是
            指令、或发送者没权限 —— 交给原有白名单流程继续处理。
        """
        if not self.config.get("vent_enabled", True):
            return False
        parsed = classify_vent(event.get_message_str())
        if not parsed:
            return False
        action, arg = parsed
        await self.vents.ensure_loaded()
        key = self._vent_key(event)
        left = self.vents.left(key)

        # 查状态：谁都能问（只读，且方便群友知道还能聊多久）
        if action == "status":
            if left > 0:
                until = time.strftime("%H:%M:%S", time.localtime(time.time() + left))
                await self._send_plain(
                    event, f"🌬 本群正在放风，剩余 {fmt_duration(left)}（{until} 结束）。"
                )
            else:
                await self._send_plain(event, "本群当前没有放风。")
            event.should_call_llm(True)
            return True

        if not await self._can_control_vent(event):
            logger.debug(
                "[llm_allowlist] vent_deny | platform=%s group=%s sender_id=%s action=%s",
                event.get_platform_name(), event.message_obj.group_id,
                event.get_sender_id(), action,
            )
            return False

        # 收风：提前结束
        if action == "close":
            if left <= 0:
                await self._send_plain(event, "本群当前没有放风。")
            else:
                await self.vents.close(key)
                await self._send_plain(event, "🌬 放风已结束，本群恢复白名单。")
            event.should_call_llm(True)
            return True

        # 放风：解析时长 → 写窗口 → 挂到期任务
        seconds = parse_duration(arg)
        if seconds <= 0:
            minutes = int(self.config.get("vent_default_minutes", _DEFAULT_VENT_MINUTES) or _DEFAULT_VENT_MINUTES)
            seconds = max(1, minutes) * 60
        limit_minutes = max(
            0, int(self.config.get("vent_max_minutes", _DEFAULT_VENT_MAX_MINUTES) or 0)
        )
        capped = bool(limit_minutes) and seconds > limit_minutes * 60
        if capped:
            seconds = limit_minutes * 60
        await self.vents.open(key, event.unified_msg_origin, seconds)
        until = time.strftime("%H:%M:%S", time.localtime(time.time() + seconds))
        if left > 0:
            text = f"🌬 放风已延长，本群放风时间重置为 {fmt_duration(seconds)}（{until} 结束）。"
        else:
            text = (
                f"🌬 放风开始！{fmt_duration(seconds)} 内本群所有人的对话都会被回答"
                f"（{until} 结束），发「收风」可提前结束。"
            )
        if capped:
            text += f"\n（超过上限，已按 {limit_minutes} 分钟封顶）"
        await self._send_plain(event, text)
        event.should_call_llm(True)
        logger.info(
            "[llm_allowlist] vent_open | platform=%s group=%s by=%s seconds=%d",
            event.get_platform_name(), event.message_obj.group_id,
            event.get_sender_id(), seconds,
        )
        return True

    # ---------------------------------------------------------------- 回复发送
    async def _send_plain(self, event: AstrMessageEvent, text: str):
        """普通回复（不引用、不 @），用于放风状态提示。"""
        try:
            await event.send(MessageChain().message(text))
        except Exception as e:
            logger.debug("[llm_allowlist] vent_reply | 发送失败: %s", e)

    async def _send_to_session(self, umo: str, text: str):
        """主动向某个会话发消息（放风到期时用，此刻没有 event）。"""
        if not umo:
            return
        try:
            await self.context.send_message(umo, MessageChain().message(text))
        except Exception as e:
            logger.debug("[llm_allowlist] vent_notify | 主动发送失败: %s", e)

    async def _send_reply(self, event: AstrMessageEvent, text: str):
        try:
            mc = MessageChain()
            mc.chain = [CompReply(id=event.message_obj.message_id), Plain(text)]
            await event.send(mc)
        except Exception:
            try:
                mc = MessageChain()
                mc.chain = [CompAt(qq=event.get_sender_id()), Plain(text)]
                await event.send(mc)
            except Exception:
                mc = MessageChain().message(text)
                await event.send(mc)

    # ---------------------------------------------------------------- handler
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        if not self._group_enabled(event):
            return

        # 放风指令优先（处理成功时会阻断默认 LLM 链路，避免指令本身又被 LLM 回一句）
        if await self._handle_vent_command(event):
            return

        if await self._is_admin_exempt(event):
            return

        if self._in_allowlist(event):
            return

        # 本群正在放风：全群临时放行，不拦也不发自定义回复
        if await self._in_vent(event):
            return

        reply = self._parse_platform_map("reply_msg").get(event.get_platform_name())
        if reply:
            await self._send_reply(event, reply)

        # 拦框架默认 LLM 链路（不再往下走），真正的兜底见 block_non_allowlist_llm
        event.should_call_llm(True)

    # ---------------------------------------------------------------- 兜底拦截
    @filter.on_llm_request(priority=maxsize)
    async def block_non_allowlist_llm(self, event: AstrMessageEvent, req) -> None:
        """取消非白名单发送者的一切 LLM 请求。

        AstrBot 只把 should_call_llm() 当作「禁止默认 LLM 请求链路」的开关
        （astr_message_event.py 原注释），拦不住 handler 自己 yield 的
        event.request_llm()（例如内置 astrbot 星的「空 @ 等待」：只 @ 机器人或
        只发唤醒词时它会先于本插件用 LLM 回一句）。而它在真正请求 provider 前会执行
        agent_sub_stages/internal.py：

            if await call_event_hook(event, EventType.OnLLMRequestEvent, req):
                return

        hook 里 stop_event() 即取消本次请求，这条门对默认链路 / 内置空 @ /
        其他插件自发的 request_llm 一视同仁，且不需要抢占 handler 优先级、
        不会阻断其他插件与指令。

        放风窗口内 _is_exempt() 恒为真，因此全群请求同样一视同仁地放行。

        Args:
            event: 当前消息事件。
            req: 即将发给 provider 的请求（此处只做放行判断，不修改）。

        Returns:
            None。命中时通过 event.stop_event() 取消请求。
        """
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if not self._group_enabled(event):
            return
        if await self._is_exempt(event):
            return
        logger.debug(
            "[llm_allowlist] block_llm | platform=%s sender_id=%s",
            event.get_platform_name(), event.get_sender_id(),
        )
        event.stop_event()
