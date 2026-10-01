import re
import time
from sys import maxsize

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Reply as CompReply, At as CompAt
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star


class LLMAllowList(Star):
    """框架默认 LLM 回复白名单。

    白名单内用户正常触发 LLM；其余用户在群里被静默跳过（可配置自定义回复）。
    除拦截框架默认 LLM 链路外，还通过 on_llm_request hook 取消一切 LLM 请求，
    因此内置「空 @ 等待」等由 handler 自发的 request_llm 也会被拦住。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._group_admins: dict[str, dict] = {}

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

    def _parse_allowlist(self):
        pmap = self._parse_platform_map("allowlist")
        return {p: {u.strip() for u in v.split(",") if u.strip()} for p, v in pmap.items()}

    def _in_allowlist(self, event: AstrMessageEvent) -> bool:
        allowlist = self._parse_allowlist()
        return str(event.get_sender_id()) in allowlist.get(event.get_platform_name(), set())

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

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """admin_bypass 打开时，判断发送者是否本群管理员/群主（仅 aiocqhttp 有效）。"""
        if not self.config.get("admin_bypass", False):
            return False
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

    async def _is_exempt(self, event: AstrMessageEvent) -> bool:
        """白名单内，或被管理员放行。"""
        return self._in_allowlist(event) or await self._is_group_admin(event)

    # ---------------------------------------------------------------- 自定义回复
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

    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_message(self, event: AstrMessageEvent):
        if await self._is_group_admin(event):
            return

        if self._in_allowlist(event):
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

        Args:
            event: 当前消息事件。
            req: 即将发给 provider 的请求（此处只做放行判断，不修改）。

        Returns:
            None。命中时通过 event.stop_event() 取消请求。
        """
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return
        if await self._is_exempt(event):
            return
        logger.debug(
            "[llm_allowlist] block_llm | platform=%s sender_id=%s",
            event.get_platform_name(), event.get_sender_id(),
        )
        event.stop_event()
